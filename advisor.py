"""OpenAI trade advisor. Reads the numbers `trade.evaluate_trade` already computed
and turns them into a verdict. The model does no arithmetic and sees no raw ESPN
payloads, so a hallucinated stat has nothing to attach to.

Calls the Chat Completions REST endpoint through httpx (already a dependency)
rather than the `openai` SDK. Every failure — no key, timeout, 4xx, bad JSON —
returns `{"available": False, "error": ...}`: the analytics on the page do not
depend on this call, and the key is never logged or returned to the browser.
"""
from __future__ import annotations

import json
import logging
from typing import Any

import httpx

log = logging.getLogger("advisor")

URL = "https://api.openai.com/v1/chat/completions"
TIMEOUT = 30.0
VERDICTS = ("accept", "decline", "counter")

SYSTEM_PROMPT = (
    "You are a fantasy football trade advisor for one manager. You are given a "
    "hypothetical trade that has already been analysed: lineup points before and "
    "after for both teams, per-player averages, recent trend, volatility, injury "
    "status and weeks remaining. Use ONLY those numbers; do not invent stats or "
    "news, and do not recompute the lineup deltas. 'you' is the manager asking. "
    "If 'prior_trades' is present, those trades are assumed already done and the rosters "
    "reflect them; 'vs_real' is each side's lineup against today's real roster, so judge "
    "the new trade as a step in that sequence. "
    "Weigh rest-of-season value, positional depth, injury and trend risk, and "
    "whether the other manager would plausibly accept. The verdict is your advice to "
    "'you' on the deal as proposed: 'accept' = it helps you and the other side could "
    "plausibly agree, so propose it; 'decline' = it hurts you or is not worth the risk, "
    "so walk away; 'counter' = the idea is sound but the terms need changing (for "
    "example it helps you so much the other side would refuse). Reply with a single JSON "
    'object: {"verdict": "accept"|"decline"|"counter", "confidence": "low"|"medium"|"high", '
    '"summary": one sentence, "reasons": [up to 4 short strings], "risks": [up to 3 short '
    'strings], "counter_offer": a specific alternative using players named in the data, '
    "or null}."
)


OUTLOOK_PROMPT = (
    "You are a fantasy football analyst for one manager. You are given a regular-season "
    "projection comparing 'baseline' (keep the roster as is) with 'simulated' (after the "
    "listed 'trades'), for the team named in 'team_name'. All numbers were computed for "
    "you: expected wins and losses, an 80% range for wins, playoff odds (the top "
    "'playoff_teams' make it), top-seed odds, expected final rank, points per week and a "
    "week-by-week win probability. Use ONLY those numbers; do not recompute them, invent "
    "news, or comment on the playoffs themselves, which are not modelled. Say plainly "
    "whether the plan is worth doing, which weeks or trade carry the gain, and what could "
    "undermine it (a thin sample, an injured acquisition, a hard stretch of opponents). "
    "Lineups are assumed optimal every week, so treat the gap between scenarios as the "
    'signal, not the absolute points. Reply with a single JSON object: {"verdict": '
    '"worth_it"|"marginal"|"not_worth_it", "headline": one sentence, "summary": 2-3 '
    'sentences, "key_changes": [up to 3 short strings], "risks": [up to 3 short strings]}.'
)
OUTLOOK_VERDICTS = ("worth_it", "marginal", "not_worth_it")


PACKAGE_PROMPT = (
    "You are a fantasy football trade advisor for one manager. 'yours' is the trade package "
    "they built (it may be null); 'candidates' are alternative packages found by searching "
    "EVERY team in the league, each with an id such as 'c1'. All numbers were computed for "
    "you: weekly lineup change for both sides, change in the manager's playoff odds and "
    "expected wins (the 'playoff_gain' and 'wins_gain' fields, as fractions), per-player "
    "average, last-3 average, trend, injury and flags. Use ONLY these numbers and ONLY the "
    "candidate ids given; never invent a player, a stat or news. First judge 'yours' against "
    "the alternatives. Then recommend up to 3 candidates, best first, weighing the size of "
    "the gain, how plausible it is that the other manager accepts (they should not lose much "
    "lineup value), and risks such as a declining or injured player or a roster drop. If no "
    "candidate beats 'yours', say so and recommend none or the closest. Reply with a single "
    'JSON object: {"your_package": {"verdict": "strong"|"fair"|"weak", "assessment": 1-2 '
    'sentences}, "recommendations": [{"candidate_id": "c1", "reason": one sentence, "risk": '
    'one short sentence, "pitch": a short, friendly message (max 2 sentences) the manager '
    "could send to the other team's owner}]}."
)
PACKAGE_VERDICTS = ("strong", "fair", "weak")


def _clean_package(raw: dict[str, Any], valid_ids: set[str]) -> dict[str, Any]:
    """Keep only recommendations that name a candidate we actually offered, once each."""
    mine = raw.get("your_package") or {}
    verdict = str(mine.get("verdict", "")).lower()
    recs, seen = [], set()
    for rec in raw.get("recommendations") or []:
        cid = str((rec or {}).get("candidate_id", ""))
        if cid not in valid_ids or cid in seen:
            continue
        seen.add(cid)
        recs.append({"candidate_id": cid, "reason": str(rec.get("reason") or ""),
                     "risk": str(rec.get("risk") or ""), "pitch": str(rec.get("pitch") or "")})
    return {
        "available": True,
        "your_package": {"verdict": verdict if verdict in PACKAGE_VERDICTS else "fair",
                         "assessment": str(mine.get("assessment") or "")},
        "recommendations": recs[:3],
    }


BRIEFING_PROMPT = (
    "You are a fantasy football analyst briefing one manager who wants to think as little as "
    "possible: tell them what to do, in order. You are given their team's status and "
    "season projection, an 'attention' list (each with an id like 'a1'), how strong each "
    "position is against the league, 'moves' (the best individual trades found across the "
    "whole league, ids like 'm1') and a 'plan' (the best chained sequence of trades, id "
    "'plan'). All numbers were computed for you; use ONLY them and only the ids given. Never "
    "invent a player, stat, injury or news, and do not recompute anything. Put urgent, "
    "free fixes (an injured starter, a better lineup) before trades. Recommend a trade only if "
    "it clearly helps; mention a roster drop or a declining player when the data shows one. "
    'Reply with a single JSON object: {"headline": one sentence on where they stand, '
    '"summary": 2-3 sentences, "actions": [up to 4 items in priority order, each '
    '{"ref": an id from the data, "do": one imperative sentence, "why": one sentence using '
    'the numbers}], "watch_out": [up to 3 short strings]}.'
)


def _clean_briefing(raw: dict[str, Any], valid_refs: set[str]) -> dict[str, Any]:
    """Keep only actions that cite something Python actually found, once each."""
    actions, seen = [], set()
    for a in raw.get("actions") or []:
        ref = str((a or {}).get("ref", ""))
        if ref not in valid_refs or ref in seen:
            continue
        seen.add(ref)
        actions.append({"ref": ref, "do": str(a.get("do") or ""), "why": str(a.get("why") or "")})
    return {"available": True, "headline": str(raw.get("headline") or ""),
            "summary": str(raw.get("summary") or ""), "actions": actions[:4],
            "watch_out": [str(x) for x in (raw.get("watch_out") or [])][:3]}


def _unavailable(error: str) -> dict[str, Any]:
    return {"available": False, "error": error}


def _clean(raw: dict[str, Any]) -> dict[str, Any]:
    verdict = str(raw.get("verdict", "")).lower()
    confidence = str(raw.get("confidence", "")).lower()
    return {
        "available": True,
        "verdict": verdict if verdict in VERDICTS else "counter",
        "confidence": confidence if confidence in ("low", "medium", "high") else "low",
        "summary": str(raw.get("summary") or ""),
        "reasons": [str(x) for x in (raw.get("reasons") or [])][:4],
        "risks": [str(x) for x in (raw.get("risks") or [])][:3],
        "counter_offer": raw.get("counter_offer") or None,
    }


def _clean_outlook(raw: dict[str, Any]) -> dict[str, Any]:
    verdict = str(raw.get("verdict", "")).lower()
    return {
        "available": True,
        "verdict": verdict if verdict in OUTLOOK_VERDICTS else "marginal",
        "headline": str(raw.get("headline") or ""),
        "summary": str(raw.get("summary") or ""),
        "key_changes": [str(x) for x in (raw.get("key_changes") or [])][:3],
        "risks": [str(x) for x in (raw.get("risks") or [])][:3],
    }


def _digest(evaluation: dict[str, Any]) -> dict[str, Any]:
    """Drop the lineup name maps — long, and the deltas already summarise them."""
    out = dict(evaluation)
    for side in ("you", "them"):
        out[side] = {k: v for k, v in evaluation[side].items()
                     if k not in ("lineup_before", "lineup_after")}
    return out


async def _ask(system: str, payload: dict[str, Any], clean, api_key: str, model: str,
               client: httpx.AsyncClient | None) -> dict[str, Any]:
    """One chat-completions round trip. Every failure becomes {"available": False}."""
    if not api_key:
        return _unavailable("OPEN_AI_KEY is not set in .env")
    body = {
        "model": model,
        "response_format": {"type": "json_object"},
        "temperature": 0.3,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(payload)},
        ],
    }
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=TIMEOUT)
    try:
        resp = await client.post(URL, json=body, headers={"Authorization": f"Bearer {api_key}"})
        if resp.status_code == 401:
            return _unavailable("OpenAI rejected the API key (401)")
        if resp.status_code == 429:
            return _unavailable("OpenAI rate limit or quota exceeded (429)")
        if resp.status_code >= 400:
            return _unavailable(f"OpenAI returned {resp.status_code}")
        content = resp.json()["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        if not isinstance(parsed, dict):
            return _unavailable("OpenAI reply was not a JSON object")
        return clean(parsed)
    except httpx.TimeoutException:
        return _unavailable("OpenAI timed out")
    except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
        log.warning("advisor failed: %s", type(exc).__name__)   # never log the request: it holds the key
        return _unavailable("OpenAI reply could not be read")
    finally:
        if owns_client:
            await client.aclose()


async def advise(evaluation: dict[str, Any], api_key: str, model: str,
                 client: httpx.AsyncClient | None = None) -> dict[str, Any]:
    return await _ask(SYSTEM_PROMPT, _digest(evaluation), _clean, api_key, model, client)


async def summarize_outlook(outlook: dict[str, Any], api_key: str, model: str,
                            client: httpx.AsyncClient | None = None) -> dict[str, Any]:
    """Plain-English read of a season outlook. The weekly rows are kept: which games
    carry the gain is part of the answer."""
    return await _ask(OUTLOOK_PROMPT, outlook, _clean_outlook, api_key, model, client)


async def recommend_package(result: dict[str, Any], api_key: str, model: str,
                            client: httpx.AsyncClient | None = None) -> dict[str, Any]:
    """Pick and explain the best of the candidates `suggest.improve_package` found.

    The model may only name candidate ids from `result`; anything else is dropped, so a
    made-up trade can never reach the page.
    """
    valid = {c["id"] for c in result.get("candidates", [])}
    if not valid:
        return _unavailable("no candidate packages to choose from")
    payload = {k: result.get(k) for k in ("team_name", "yours", "candidates", "prior_trades")}
    return await _ask(PACKAGE_PROMPT, payload, lambda raw: _clean_package(raw, valid),
                      api_key, model, client)


async def summarize_briefing(payload: dict[str, Any], valid_refs: set[str], api_key: str, model: str,
                             client: httpx.AsyncClient | None = None) -> dict[str, Any]:
    """The written briefing. Generated only when the user asks; see app.py."""
    return await _ask(BRIEFING_PROMPT, payload, lambda raw: _clean_briefing(raw, valid_refs),
                      api_key, model, client)
