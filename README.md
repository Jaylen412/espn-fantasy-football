# ESPN Live Fantasy Draft Dashboard

A local-only dashboard that mirrors an in-progress ESPN fantasy football draft, shows whose turn it is
and what everyone has taken, and ranks the best available players for your next pick.

Read-only — it never drafts for you. Build spec: [`MD/REQUIREMENTS.md`](MD/REQUIREMENTS.md).

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Fill in `.env`:

- `ESPN_LEAGUE_ID` — the integer in your `fantasy.espn.com` URL.
- `ESPN_S2` / `ESPN_SWID` — private leagues only. Chrome DevTools → Application → Cookies →
  `https://fantasy.espn.com`. Keep the braces on `SWID`.
- `MY_TEAM_ID` — run `python scripts/whoami.py`, find your row, paste the id.

## Before the draft — do these in order

```bash
python scripts/dump_samples.py          # 1. dump real responses; confirm every [VERIFY] item (§5.3)
python scripts/whoami.py                # 2. find MY_TEAM_ID
python scripts/probe_draft.py --record samples/live/
                                        # 3. join a Mock Draft Lobby and measure REST lag (§5.5)
```

Step 3 is the one that matters. It settles the highest-risk assumption in the whole design — how far
behind ESPN's REST replica runs versus the draft room UI — and `--record` saves the responses as
replay fixtures at the same time.

Real dumps go to `samples/live/`; the synthetic fixture set lives in `samples/`. Keeping them apart
means neither can overwrite the other. Everything under `samples/` is gitignored — a real dump is the
only copy you will ever have of it.

## Running

```bash
uvicorn app:app --host 127.0.0.1 --port 8000        # live
python app.py --replay samples/                      # offline, from recorded fixtures
```

Open <http://127.0.0.1:8000>. Allow notifications when prompted — that is how you get told you are on
the clock while looking at another window.

> `uvicorn app:app --replay samples/` from the spec does not work: uvicorn rejects unknown flags.
> Use `python app.py --replay samples/`, or set `REPLAY_DIR=samples` in `.env` and start uvicorn normally.

No credentials? The synthetic fixtures in `samples/` drive the whole UI offline, all the way to a
completed draft — which is what makes the team report worth looking at. Regenerate them any time:

```bash
python scripts/make_fixtures.py
MY_TEAM_ID=11 REPLAY_DIR=samples POLL_INTERVAL_SECONDS=2 uvicorn app:app --port 8000
```

## Reading the dashboard

- **Left** — every pick, newest first. Yours are outlined in green.
- **Center** — who is on the clock. The pane pulses green and the tab title changes on your turn.
  Below it, how many picks until you are up.
- **Right** — the top 8 available, each with a one-line reason, then best-available tabs by position.
- **Top right** — the connection dot. Green means a poll landed within 15s. Red, plus a banner, means
  the board may be out of date. Believe the banner.

## Recommendations

`score = w1·VOR + w2·scarcity + w3·need − w4·survival`, normalised to roughly 0–100. VOR is the
backbone — it makes a TE1 and a WR3 comparable. Subtracting survival probability is the point of the
tool: it pushes you toward the player who will *not* come back to you at your next pick.

Weights live in `WEIGHTS` at the top of `recommender.py`. They are meant to be edited mid-draft — with
`--reload` on, saving the file re-ranks the board within a poll.

## Team report — grades, leverage, trades

The **team report** link in the header (or `/analysis`) opens the after-the-fact view. The draft board
tells you who to take next; this tells you how the picks you already made turned out and what to do
with the roster they built.

- **Every pick, graded.** A letter and a sentence explaining it: whether he fell past his ESPN rank or
  you reached, how much of the board's remaining value the pick captured, and whether it filled an open
  starting slot. Where a better player was still sitting there, it names him — and says whether he was
  *still* available at your next turn, which is what settles the argument.
- **Kickers and defenses are graded against other kickers and defenses.** Every roster has to carry
  one, and their ranks sit hundreds of picks below where anyone takes them; measured against the whole
  board every K in the league would grade F. What is graded is whether you took the best one left, and
  whether a real starting slot was still empty when you did.
- **How to leverage it.** Your best lineup by projection (not by draft order), where it ranks in the
  league, which positions beat the league average and which do not, and your surplus — the bench
  players worth more than the best man left on the board, which is the only thing you can trade without
  weakening your starters.
- **Who to call.** Teams whose surplus covers your hole *and* whose hole your surplus covers, with a
  concrete give/get and a note on whether the two sides are close enough to send as-is. Fit is a
  geometric mean of what each side gains, so a deal only you like scores near zero. Expect this to stay
  empty until late in the draft — before then nobody has surplus yet.
- The team dropdown reads any team's report, so you can check the other side before you offer.

It is computed on demand at `GET /api/analysis` (`?team_id=` optional), never in the poll loop — the
SSE snapshot stays small and the draft board is unaffected.

## Testing

```bash
pytest                            # 69 tests
pytest tests/test_analysis.py     # team report — in-memory fixtures, no files needed
pytest tests/test_draft_math.py::test_snake_reverses_every_other_round
```

Covers snake order, `picks_until_my_next_turn`, VOR and replacement level, local availability,
cold-start reconstruction, the §9 failure paths (transient outage, 401, rate limiting), and pick
grading, lineup optimisation and trade fits.

Most of the suite reads the synthetic fixtures in `samples/`. If they are missing, regenerate:

```bash
python scripts/make_fixtures.py                       # 322 players, a whole 12-team draft
python scripts/make_fixtures.py --frames 40           # just the first 40 picks
python scripts/make_fixtures.py --out /tmp/replay     # leave samples/ alone entirely
```

It refuses to write into a directory holding JSON it did not generate, so it cannot eat the real dump
in `samples/live/`. `tests/conftest.py` keeps the suite from appending to your real `draft_log.jsonl`.

## If something breaks mid-draft

| Symptom | What it means |
|---|---|
| Red 401 banner | Cookies expired. Re-copy `espn_s2` / `SWID` into `.env`, restart. |
| Red STALE banner | Polls are failing. Picks shown are old. Check `/api/health`. |
| Picks appear, no recommendations | The player pool call failed. The board still works; ranks do not. |
| "Unknown player 12345" | Player missing from the pool. The pick is still recorded. |

`GET /api/health` reports poller status, seconds since the last successful fetch, and the consecutive
error count. Every pick is appended to `draft_log.jsonl`; unexpected response bodies land in `errors/`.

Restarting is safe at any point — state is rebuilt in full from `mDraftDetail`.
