# ESPN Live Fantasy Draft Dashboard — Technical Requirements

A local-only web dashboard that mirrors an in-progress ESPN fantasy football draft, shows whose turn it is and what everyone has taken, and ranks the best available players for the user's next pick.

This document is the build spec. Anything marked **[VERIFY]** is based on ESPN's undocumented API and must be confirmed against a live response before it is relied on.

---

## 1. Context and constraints

- **Deadline-sensitive.** The draft is live and one-shot. Prefer a working narrow build over an elegant broad one. If a feature is not ready, degrade to showing raw data rather than crashing.
- **ESPN has no official public fantasy API.** Everything here uses the undocumented v3 endpoints that power the ESPN web app. They can change without notice.
- **Read-only.** This app never makes a pick, never writes to ESPN. It observes and recommends. The user still clicks draft in the ESPN UI.
- **Local only.** No deployment, no auth layer, no multi-user support. Binds to `127.0.0.1`.
- **Single user.** Only the owner's browser opens it.

### Non-goals

- Auto-drafting or any write operation against ESPN
- Supporting Yahoo, Sleeper, NFL.com, or other platforms
- Persisting history across drafts (in-memory state is fine, plus an append-only log file for post-mortem)
- Auction drafts (snake only, unless the config says otherwise — see §6)

---

## 2. Tech stack

| Layer | Choice | Notes |
|---|---|---|
| Language | Python 3.11+ | 3.11 or newer for `tomllib` and better async ergonomics |
| Backend | FastAPI + Uvicorn | Needed for the Server-Sent Events endpoint |
| HTTP client | `httpx` | Connection pooling and timeouts; `requests` acceptable if simpler |
| Frontend | Single static `index.html` — vanilla JS + Tailwind via CDN | No build step. A build step is a liability under time pressure. |
| Live updates | Server-Sent Events (SSE) | One-directional server→browser is all that's needed. Avoid WebSockets. |
| Config | `.env` via `python-dotenv` | Never commit |
| Optional | `espn_api` (PyPI) | Handles cookie auth and object mapping. Convenient for the player pool; likely too slow/coarse for the live poll loop. See §5.4. |

```
fastapi
uvicorn[standard]
httpx
python-dotenv
```

**Why a backend at all:** ESPN's API does not send permissive CORS headers, so browser JavaScript cannot call it directly. The Python process is a required proxy, not an architectural preference.

---

## 3. Prerequisites

1. Python 3.11+ available on PATH
2. An active ESPN fantasy football league the user is a member of
3. Chrome or Firefox, for extracting cookies
4. The league's ID, scoring format, team count, and roster slots

---

## 4. Configuration

Create `.env` at the project root. Ship a `.env.example` with the same keys and empty values.

```bash
# Required
ESPN_LEAGUE_ID=            # integer, from the fantasy.espn.com URL
ESPN_SEASON=2026
ESPN_S2=                   # cookie, private leagues only
ESPN_SWID=                 # cookie, private leagues only — include the braces: {ABC-123-...}

# Who "I" am, so the dashboard knows which picks are mine
MY_TEAM_ID=                # ESPN internal team id, not the display name

# League shape — used by the recommendation engine
SCORING_FORMAT=PPR         # PPR | HALF_PPR | STANDARD
TEAM_COUNT=12
ROSTER_SLOTS=QB:1,RB:2,WR:2,TE:1,FLEX:1,DST:1,K:1,BENCH:7

# Tuning
POLL_INTERVAL_SECONDS=4
LOG_LEVEL=INFO
```

### Getting the cookies

Log into ESPN in Chrome, then DevTools → Application → Storage → Cookies → `https://fantasy.espn.com`. Copy the values of `espn_s2` and `SWID`. They persist across sessions, so this is a one-time step. `SWID` includes surrounding braces — keep them.

### Getting `MY_TEAM_ID`

Call the `mTeam` view (§5.2) and match on the owner's display name. Build a small `scripts/whoami.py` that prints every team's `id`, `name`, and owner so the user can read theirs off and paste it into `.env`.

---

## 5. ESPN API reference

### 5.1 Base

```
https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/{SEASON}/segments/0/leagues/{LEAGUE_ID}
```

Views are appended as query params and **can be stacked**: `?view=mDraftDetail&view=mTeam&view=mSettings`.

**Required headers**

```
Cookie: espn_s2={ESPN_S2}; SWID={ESPN_SWID}
Accept: application/json
User-Agent: <a normal browser UA string>
```

A default Python user-agent sometimes gets a non-JSON response. Set a browser-like UA.

### 5.2 Views used

| View | Purpose | Poll frequency |
|---|---|---|
| `mDraftDetail` | The pick list — the core live feed | Every `POLL_INTERVAL_SECONDS` |
| `mTeam` | Team IDs, names, owners | Once at startup |
| `mSettings` | Draft order, roster composition, scoring rules | Once at startup |
| `kona_player_info` | Full player pool with ranks and projections | Once at startup, refresh every ~60s |

### 5.3 Expected shapes **[VERIFY]**

`mDraftDetail` returns a `draftDetail` object containing `drafted` (bool), `inProgress` (bool), and `picks` (array). Each pick is expected to carry roughly:

```json
{
  "id": 1,
  "overallPickNumber": 1,
  "roundId": 1,
  "roundPickNumber": 1,
  "teamId": 4,
  "playerId": 4362628,
  "keeper": false,
  "autoDraftTypeId": 0
}
```

**The first task of the build is to dump one real response to `samples/draft_detail.json` and confirm these field names.** Do not write the parser from this document alone.

Player position mapping (`defaultPositionId`), also **[VERIFY]**:

```
1=QB  2=RB  3=WR  4=TE  5=K  16=D/ST
```

Lineup slot IDs (`lineupSlotId`), also **[VERIFY]**:

```
0=QB  2=RB  4=WR  6=TE  16=D/ST  17=K  20=Bench  21=IR  23=FLEX
```

### 5.4 The player pool

`kona_player_info` requires an `X-Fantasy-Filter` header containing JSON. This is what makes the whole thing work — the response includes each player's draft rank, ownership percentage, and **projected points already adjusted for the league's own scoring settings**, which is far better than generic external rankings.

```
X-Fantasy-Filter: {
  "players": {
    "filterStatus": {"value": ["FREEAGENT", "WAIVERS"]},
    "filterSlotIds": {"value": [0,2,4,6,16,17,23]},
    "sortDraftRanks": {"sortPriority": 100, "sortAsc": true, "value": "PPR"},
    "limit": 400
  }
}
```

Notes:
- `sortDraftRanks.value` must be `"PPR"` or `"STANDARD"` — map from `SCORING_FORMAT`.
- Each player carries `draftRanksByRankType` keyed by `STANDARD` / `PPR`, each with a `rank` and `auctionValue`.
- `limit` above 400 sometimes returns errors. Cap it and paginate if more is needed.
- **Do not trust `filterStatus` to reflect draft state.** During a live draft, ESPN's free-agent status may lag. Compute availability locally: `available = all_players - drafted_player_ids`. This is the single most important correctness rule in the app.

### 5.5 Live-update behavior **[VERIFY — highest risk item]**

ESPN's real draft room uses a WebSocket. The REST view reads from a replica, so picks appear there with some lag. Polling at 3–5 seconds should be adequate, but **this is unproven and must be tested against ESPN's Mock Draft Lobby before the real draft**. Build `scripts/probe_draft.py`, which polls `mDraftDetail` once a second, prints every new pick with a wall-clock timestamp, and lets the user eyeball the delay against the ESPN UI.

If REST lag turns out to be unusable, the fallback is a browser-side scraper reading the ESPN draft room DOM. Do not build this preemptively.

---

## 6. Architecture

```
espn_client.py     Thin HTTP wrapper. Cookies, headers, retries, timeouts.
poller.py          Background asyncio task. Polls mDraftDetail, diffs, emits events.
state.py           In-memory DraftState. Single source of truth.
recommender.py     Scores available players for the user's next pick.
app.py             FastAPI. Serves static/, exposes /api/*, /events.
static/index.html  The dashboard.
```

### Flow

1. **Startup** — load config, fetch `mTeam` + `mSettings` + `kona_player_info`, build `DraftState`, reconstruct the full snake order.
2. **Poll loop** — every N seconds fetch `mDraftDetail`. Diff `picks` against known picks by `overallPickNumber`. For each new pick, append to state, remove the player from the available pool, append a line to `draft_log.jsonl`.
3. **On change** — recompute recommendations, push a state snapshot over SSE.
4. **Browser** — subscribes to `/events`, re-renders on each snapshot. Full snapshots, not deltas; the payload is small and full snapshots are far easier to debug live.

### Endpoints

| Route | Returns |
|---|---|
| `GET /` | The dashboard HTML |
| `GET /api/state` | Current full snapshot (also the SSE fallback if EventSource fails) |
| `GET /events` | SSE stream of snapshots |
| `GET /api/health` | Poller status, last successful fetch timestamp, consecutive error count |

### Snapshot payload

```json
{
  "draft_status": "IN_PROGRESS",
  "current_pick": {"overall": 27, "round": 3, "team_id": 4, "team_name": "...", "is_me": true},
  "next_my_pick": {"overall": 30, "picks_away": 3},
  "picks": [{"overall": 1, "round": 1, "team_name": "...", "player_name": "...", "position": "RB"}],
  "my_roster": {"QB": [], "RB": ["..."], "WR": [], "TE": [], "FLEX": [], "DST": [], "K": [], "BENCH": []},
  "recommendations": [
    {"name": "...", "position": "WR", "team": "SF", "espn_rank": 18,
     "projected_points": 214.5, "vor": 41.2, "survival_prob": 0.31,
     "score": 88.4, "reason": "Top WR left; unlikely to last 12 picks"}
  ],
  "best_available_by_position": {"RB": [], "WR": [], "TE": [], "QB": []},
  "last_updated": "2026-09-05T18:04:12Z",
  "connection_healthy": true
}
```

---

## 7. Recommendation engine

Raw ESPN rank is not a recommendation — it ignores roster construction and positional scarcity. Score each available player as:

```
score = w1 * VOR_normalized
      + w2 * scarcity_bonus
      + w3 * roster_need
      - w4 * survival_probability
```

**Value over replacement (VOR).** The replacement level for a position is the projected points of the player at index `TEAM_COUNT * starters_at_position` in that position's remaining list. `VOR = player_projected_points - replacement_points`. This is the backbone of the score — it is what makes a TE1 and a WR3 comparable on one axis.

**Survival probability.** Given `picks_until_my_next_turn` (derivable exactly from the snake order — no estimation needed), estimate the chance the player is still there. A reasonable v1: `P(survive) = max(0, 1 - picks_until_next / max(1, player_rank - current_pick))`, clamped to `[0,1]`. Subtracting this is the point of the whole tool — it says *take the guy who won't come back to you, not simply the highest-ranked guy*.

**Roster need.** Multiplier that rises as unfilled starting slots at that position shrink, and collapses toward zero once starters are filled and only bench spots remain.

**Positional scarcity.** Bonus proportional to the drop-off between this player and the next-best at the same position — the "cliff."

Weights live in one dict at the top of `recommender.py` so they can be tuned mid-draft. Each recommendation must include a one-line human-readable `reason`; a number with no explanation is useless when there are 45 seconds on the clock.

---

## 8. Dashboard UI

Three panes, readable across a room, no interaction required to see the important thing.

- **Left — draft board.** Reverse-chronological pick feed. Team name, player, position, round. The user's own picks visually distinct.
- **Center — on the clock.** Large. Current team name, pick number, round. When it is the user's turn, the entire pane changes color and the browser tab title updates. Below it: "Your next pick: #30, 3 picks away."
- **Right — recommendations.** Top 8 available, ranked, each with position, projection, and the `reason` string. Below that, best-available tabs by position.

Requirements:
- Fires a browser notification and an audio cue when the user's turn arrives (request notification permission on load)
- A visible connection indicator — green when the last poll succeeded within 15s, red otherwise. **Silent staleness is the worst possible failure mode.** A red banner is better than confidently rendering a board that is four picks out of date.
- Dark background; this runs for three hours

---

## 9. Error handling

| Failure | Behavior |
|---|---|
| ESPN returns 401 | Cookies expired. Render a clear banner telling the user to re-copy `espn_s2` / `SWID`. Do not retry silently. |
| ESPN returns 429 | Back off exponentially, cap at 30s, keep serving last known state. |
| Timeout / 5xx | Retry with backoff. Mark connection unhealthy after 3 consecutive failures. |
| Unknown `playerId` | Show the raw ID rather than dropping the pick. Never lose a pick. |
| Response shape changed | Log the raw body to `errors/` and keep serving last known good state. |

The poller must never die. Wrap the loop body in a broad `try/except`, log, and continue. A crashed poller mid-draft is the one unrecoverable outcome.

---

## 10. Running locally

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env             # then fill it in

python scripts/whoami.py         # prints team IDs; paste yours into MY_TEAM_ID
uvicorn app:app --host 127.0.0.1 --port 8000 --reload
```

Open `http://127.0.0.1:8000`.

---

## 11. Testing

Ordered by importance. Item 1 is worth more than the rest combined.

1. **Live mock draft.** Join ESPN's Mock Draft Lobby and run the dashboard against it end to end. This validates the riskiest assumption (§5.5) under real conditions. Do this first, before polishing anything.
2. **Recorded fixture replay.** Save the sequence of `mDraftDetail` responses from the mock into `samples/`. Add a `--replay samples/` flag that feeds them to the poller on a timer, so the UI and recommender can be developed offline without burning requests.
3. **Unit tests** for snake order generation, `picks_until_my_next_turn`, and VOR calculation. These are pure functions and cheap to cover.
4. **Cold start mid-draft.** Kill and restart the server after several picks. It must rebuild full state from `mDraftDetail` and resume correctly — this is the actual recovery path if something crashes live.

---

## 12. Acceptance criteria

- [ ] Authenticates against a private league and fetches the draft without error
- [ ] New picks appear on the dashboard within ~5 seconds of appearing in ESPN's UI
- [ ] Correctly identifies whose turn it is and how many picks until the user's next
- [ ] Every drafted player is removed from the available pool, verified locally, not via ESPN's status field
- [ ] Recommendations account for current roster needs and update after every pick
- [ ] Clear audible and visual alert when it is the user's turn
- [ ] Connection health is always visible; stale data is never shown as fresh
- [ ] Server survives a transient ESPN outage and recovers on its own
- [ ] Restarting the server mid-draft reconstructs full state

---

## 13. Build order

Under time pressure, in this order, stopping wherever the clock runs out:

1. `espn_client.py` + dump real responses to `samples/` — confirm every **[VERIFY]** item
2. `scripts/probe_draft.py` against a mock draft — settle the polling-latency question
3. `state.py` + poller + `/api/state` returning correct picks and turn order
4. Minimal HTML that polls `/api/state` every 3s and renders the board
5. Recommender v1: VOR only
6. SSE, alerts, styling
7. Survival probability and scarcity weighting

Steps 1–4 are the product. Everything after is upside.
