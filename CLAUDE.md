# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Source of truth

**[`MD/REQUIREMENTS.md`](MD/REQUIREMENTS.md) is the build spec.** Read the relevant section before writing code in that
area — this file is a map and a set of durable rules, not a replacement. Anything the spec marks **[VERIFY]** is
inferred from an undocumented API and must be confirmed against a live response first.

| Question | Spec section |
|---|---|
| Scope, non-goals, why this is deadline-shaped | §1 |
| Stack choices and `requirements.txt` | §2 |
| `.env` keys, cookie extraction, finding `MY_TEAM_ID` | §4 |
| Endpoint base, headers, views, response shapes, `X-Fantasy-Filter` | §5 |
| Module layout, data flow, routes, snapshot payload | §6 |
| Scoring formula, VOR, survival probability | §7 |
| Three-pane UI, alerts, connection indicator | §8 |
| Per-failure error behavior | §9 |
| Local run commands | §10 |
| Test priorities (mock draft first) | §11 |
| Acceptance checklist | §12 |
| Build order under time pressure | §13 |

## Current state

Built and passing 46 tests against synthetic fixtures. **Never run against a live ESPN league** — every `[VERIFY]`
item in §5.3/§5.4/§5.5 is still unconfirmed, and the parsers were written from the spec, which §5.3 explicitly warns
against. `scripts/dump_samples.py` exists to close that gap; run it before trusting anything. Not a git repository yet.

## What this is

A local-only FastAPI dashboard that mirrors an in-progress ESPN fantasy football draft: it polls ESPN's undocumented v3
API, tracks who has taken whom, and ranks the best available players for the user's next pick. Read-only — it never
writes to ESPN. Single user, binds `127.0.0.1`, no build step, no deployment (§1).

The draft is a one-shot live event. Prefer a narrow working build over a broad elegant one; degrade to showing raw data
rather than crashing.

## Planned commands

Per §10, plus the two scripts the spec calls for in §4 and §5.5:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt pytest pytest-asyncio
cp .env.example .env                                    # then fill in league ID + cookies (§4)

python scripts/dump_samples.py                          # build step 1: confirm [VERIFY] items (§13)
python scripts/whoami.py                                # prints team IDs → paste yours into MY_TEAM_ID
python scripts/probe_draft.py --record samples/         # 1s poll; measures REST lag (§5.5)
python scripts/make_fixtures.py                         # synthetic fixtures, no credentials needed

uvicorn app:app --host 127.0.0.1 --port 8000 --reload   # live
python app.py --replay samples/                         # offline replay (§11.2)

pytest                                                  # 46 tests
pytest tests/test_draft_math.py::test_snake_reverses_every_other_round
```

**Spec deviation:** `uvicorn app:app --replay samples/` (§10) cannot work — uvicorn rejects unknown flags. Replay goes
through `python app.py --replay DIR` or the `REPLAY_DIR` env var.

## Architecture (§6)

```
config.py          .env loading, position/slot maps, roster shape
espn_client.py     HTTP wrapper — cookies, browser UA, retries, timeouts
poller.py          asyncio task: polls mDraftDetail, diffs picks, emits events
state.py           In-memory DraftState — single source of truth
recommender.py     Scores available players for the user's next pick
app.py             FastAPI: serves static/, /api/state, /api/health, /events (SSE)
static/index.html  Vanilla JS + Tailwind CDN dashboard
scripts/           dump_samples, whoami, probe_draft, make_fixtures
```

`poller.py` abstracts its data behind a `Source` protocol — `LiveSource` (ESPN) or `ReplaySource` (fixtures from
`samples/`), which is what makes offline development and the failure-path tests possible.

Data flow: startup fetches `mTeam` + `mSettings` + `kona_player_info` and reconstructs the full snake order → the poll
loop diffs `draftDetail.picks` by `overallPickNumber`, appends new picks to state and to `draft_log.jsonl` → each change
recomputes recommendations and pushes a **full snapshot** over SSE (not deltas — small payload, far easier to debug
live). The browser re-renders on each snapshot. Snapshot shape is specified in §6.

A Python backend is required, not preferred: ESPN sends no permissive CORS headers, so browser JS cannot call the API
directly (§2).

## MD/DESIGN.md does not apply to the dashboard

`MD/DESIGN.md` is an "Earlydog" Bauhaus brand kit — light cream canvas, 116px display type, marketing-site section
gaps, for a DevOps product. It is **not** the style reference for this dashboard, and the user confirmed this. §8
requires a dark background because the tool runs for three hours, and the layout is deliberately dense rather than
spacious. Do not restyle the dashboard toward that kit.

## Rules that bite if broken

- **Compute availability locally.** `available = all_players - drafted_player_ids`. ESPN's `filterStatus` free-agent
  field lags during a live draft; never derive availability from it. §5.4 calls this the single most important
  correctness rule in the app.
- **The poller must never die.** Broad `try/except` around the loop body, log, continue. A crashed poller mid-draft is
  the one unrecoverable outcome (§9).
- **Never show stale data as fresh.** Connection indicator goes red when the last successful poll is >15s old. Silent
  staleness is the worst failure mode (§8).
- **Never lose a pick.** Unknown `playerId` renders as the raw ID rather than being dropped (§9).
- **Verify shapes against live responses.** Pick fields, the `defaultPositionId` / `lineupSlotId` maps (§5.3), and
  especially REST polling lag (§5.5, the highest-risk item) are unconfirmed. Dump real responses to `samples/` before
  writing a parser from the doc.
- **401 means expired cookies.** Surface a banner telling the user to re-copy `espn_s2` / `SWID`; do not retry silently.
  429 backs off exponentially, capped at 30s, still serving last known state (§9).
- `.env` is never committed; `.env.example` ships with the same keys and empty values. `SWID` keeps its surrounding
  braces (§4).

## ESPN API notes (§5)

Base: `https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/{SEASON}/segments/0/leagues/{LEAGUE_ID}`

Views stack as repeated query params (`?view=mDraftDetail&view=mTeam&view=mSettings`). Requests need the cookie header
and a browser-like `User-Agent` — a default Python UA can return non-JSON. `kona_player_info` additionally needs an
`X-Fantasy-Filter` JSON header (exact filter in §5.4); it is the payload that makes the tool work, since its projections
are already adjusted for the league's own scoring settings. Cap its `limit` at 400.

## Recommendation engine (§7)

`score = w1*VOR_normalized + w2*scarcity_bonus + w3*roster_need - w4*survival_probability`, with weights in one dict at
the top of `recommender.py` so they can be tuned mid-draft. VOR is the backbone — it is what makes a TE1 and a WR3
comparable. Subtracting survival probability is the point of the tool: take the player who won't come back to you, not
the highest-ranked one. Every recommendation carries a one-line human-readable `reason`; a bare number is useless with
45 seconds on the clock.

## Build order (§13)

`espn_client` + real samples → `probe_draft.py` against a mock draft → `state.py` + poller + `/api/state` → minimal
polling HTML. Those four are the product; recommender, SSE, alerts, and scarcity weighting are upside. Validate against
ESPN's Mock Draft Lobby end to end before polishing anything (§11.1).
