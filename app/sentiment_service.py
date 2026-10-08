"""
Shared sentiment-computation logic. Both the HTTP endpoints (app/main.py --
x402 and subscription lanes) and the background alert poller (app/alerts.py)
call this so there is exactly one code path computing a score.
"""

import asyncio
import json
import logging
from datetime import datetime, timezone

from fastapi import HTTPException

from app.sources.news import fetch_feed_items, fetch_news_headlines, select_headlines
from app.sources.feargreed import fetch_fear_greed
from app import window
from app.coins import UnsupportedSymbol, resolve_name, supported_symbol, validate_symbol

logger = logging.getLogger(__name__)


def require_supported_symbol(symbol: str, lane: str) -> str:
    """The normalized symbol, for a route that sells or meters a reading.
    400 for a malformed symbol; 404 for a well-formed one this API doesn't
    score, with one "unsupported_symbol" JSON log line (grep the logs for it
    to see what people ask for). Call it before anything is charged: the
    x402 middleware never settles a payment on a 4xx."""
    try:
        return supported_symbol(symbol)
    except UnsupportedSymbol as e:
        logger.info(json.dumps({
            "event": "unsupported_symbol",
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "symbol": e.symbol,
            "lane": lane,
        }))
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


INSUFFICIENT_NEWS_RESPONSE = {
    422: {
        "description": "Not enough recent news for this coin to give a reliable "
        f"reading (effective sample size below {window.MIN_EFFECTIVE_SAMPLE} in the "
        f"last {window.MAX_AGE_HOURS} hours). No reading is returned. Not charged."
    }
}


def refuse_if_insufficient(payload: dict, lane: str) -> None:
    """Raise a 422 (and log one "insufficient_news_refused" JSON line) when
    `payload` carries window.INSUFFICIENT_LABEL: we don't charge for answers
    we don't have. Call it after computing the reading and before anything
    is counted; the x402 middleware never settles a payment on a 4xx."""
    overall = payload.get("overall_sentiment") or {}
    if overall.get("label") != window.INSUFFICIENT_LABEL:
        return
    symbol = payload["symbol"]
    effective_n = overall.get("effective_sample_size") or 0.0
    logger.info(json.dumps({
        "event": "insufficient_news_refused",
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "symbol": symbol,
        "lane": lane,
        "effective_n": effective_n,
    }))
    # Don't send someone asking about BTC or ETH to BTC or ETH.
    if symbol in ("BTC", "ETH"):
        suggestion = "Try again later."
    else:
        suggestion = f"Try BTC or ETH, or {symbol} again later."
    raise HTTPException(
        status_code=422,
        detail=f"Not enough recent news for {symbol} in the last {window.MAX_AGE_HOURS} hours "
        f"to give a reliable reading (effective sample size {effective_n:.1f}, need "
        f"{window.MIN_EFFECTIVE_SAMPLE}). Not charged. {suggestion}",
    )

SOURCES = [
    "coindesk.com RSS",
    "cointelegraph.com RSS",
    "decrypt.co RSS",
    "bitcoinmagazine.com RSS",
    "theblock.co RSS",
    "cryptoslate.com RSS",
    "newsbtc.com RSS",
    "cryptopotato.com RSS",
    "thedefiant.io RSS",
    "alternative.me Fear & Greed Index",
]


MAX_DRIVERS = 5
_POSITIVE, _NEGATIVE = window.POSITIVE_AT, window.NEGATIVE_AT
# Kept for callers that imported it from here.
SCORED_CHARS = window.SCORED_CHARS


def top_drivers(scored: list, limit: int = MAX_DRIVERS) -> list:
    """The headlines that moved the score most. The score is a weighted
    average, so a headline's effect on it is weight * score / total weight:
    drivers rank by the size of that effect, not by score alone (an older
    headline with a strong score can move it less than a newer, milder one).
    Headlines with zero effect are left out. Titles and links only, never
    the description (article text). `scored` is window.in_window(...)."""
    total_weight = sum(s.weight for s in scored)
    ranked = [(s.weight * s.score / total_weight, i, s) for i, s in enumerate(scored)] if total_weight else []
    ranked = [entry for entry in ranked if entry[0] != 0]
    ranked.sort(key=lambda entry: (-abs(entry[0]), entry[1]))
    return [
        {
            "title": s.headline.title,
            "source": s.headline.source,
            "link": s.headline.link,
            "published": s.headline.published,
            "score": round(s.score, 4),
            "weight": round(s.weight, 4),
            "effect": round(effect, 4),
        }
        for effect, _, s in ranked[:limit]
    ]


def drivers_summary(symbol: str, drivers: list) -> str:
    """One plain line, e.g. "3 of the top 5 headlines are negative, 2 are positive"."""
    n = len(drivers)
    if n == 0:
        return f"No headlines about {symbol} from the last {window.MAX_AGE_HOURS} hours moved the score."
    kinds = [
        "negative" if d["score"] <= _NEGATIVE else "positive" if d["score"] >= _POSITIVE else "neutral"
        for d in drivers
    ]
    # Most common first; a tie goes to the kind of the higher-ranked headline.
    parts = sorted(
        ((kinds.count(k), k) for k in dict.fromkeys(kinds)), key=lambda p: (-p[0], kinds.index(p[1]))
    )
    if n == 1:
        return f"The top headline is {parts[0][1]}."
    if len(parts) == 1:
        return f"All {n} top headlines are {parts[0][1]}."
    first_count, first_kind = parts[0]
    verb = "is" if first_count == 1 else "are"
    rest = ", ".join(f"{c} {'is' if c == 1 else 'are'} {k}" for c, k in parts[1:])
    return f"{first_count} of the top {n} headlines {verb} {first_kind}, {rest}."


def build_payload(symbol: str, headlines: list, fear_greed, now=None) -> dict:
    """Score `headlines` (already matched to `symbol`) over the time window
    in app/window.py."""
    scored = window.in_window(headlines, now)
    overall = window.summarize(scored)
    drivers = top_drivers(scored)
    return {
        "symbol": symbol,
        "name": resolve_name(symbol),
        "overall_sentiment": overall,
        "drivers": drivers,
        "drivers_summary": drivers_summary(symbol, drivers),
        "breakdown": {
            "news": overall,
            "fear_greed_index": fear_greed,
        },
        "sources": list(SOURCES),
    }


async def compute_sentiment_payload(symbol: str) -> dict:
    """Raises ValueError on an invalid symbol."""
    symbol = validate_symbol(symbol)

    news_task = fetch_news_headlines(symbol, resolve_name(symbol))
    fng_task = fetch_fear_greed()

    headlines, fear_greed = await asyncio.gather(news_task, fng_task)
    return build_payload(symbol, headlines, fear_greed)


async def fetch_inputs():
    """(feed items, Fear & Greed) from ONE fetch of each."""
    return await asyncio.gather(fetch_feed_items(), fetch_fear_greed())


def payloads_from(items: list, fear_greed, symbols: list, now=None) -> dict:
    """{symbol: payload} scored from already-fetched feed items."""
    symbols = [validate_symbol(s) for s in symbols]
    return {s: build_payload(s, select_headlines(items, s), fear_greed, now) for s in symbols}


async def compute_sentiment_payloads(symbols: list) -> dict:
    """{symbol: payload} for several symbols from ONE fetch of the feeds
    and the Fear & Greed Index, so the request count doesn't grow with the
    number of symbols. Raises ValueError on an invalid symbol."""
    symbols = [validate_symbol(s) for s in symbols]
    items, fear_greed = await fetch_inputs()
    return payloads_from(items, fear_greed, symbols)
