"""
Traction-focused aggregate endpoints, built entirely on the existing
per-coin sentiment pipeline (app/sentiment_service.py, app/archive.py):

  GET /market-sentiment  (free, unauthenticated; wired up in app/main.py)
    -- one market-wide sentiment index, aggregated from the same per-coin
    readings /sentiment/{symbol} sells, weighted by each coin's
    effective_sample_size so a quiet-news day for thin altcoins doesn't
    swing it. Modeled on Alternative.me's Fear & Greed Index: free and
    embeddable (bot dashboards, Discord/Telegram bots, trackers) so it
    drives discovery back to the paid /sentiment/{symbol} endpoint, not a
    replacement for its per-coin detail.

  GET /movers  (paid, x402; wired up in app/main.py) -- ranks coins by
    sentiment SHIFT over the last ~24h: the current reading (shared hourly
    cache, same as /market-sentiment) vs. the
    archived hourly reading closest to 24h ago (app.archive.hourly_rows).
    One call answers the question an agent scanning for opportunities
    actually wants, instead of looping /sentiment over every symbol --
    no new data source, just aggregation of what's already computed.

Both reuse app.window's canonical constants and thresholds rather than
redefining them, so they always agree with /sentiment's own "insufficient
recent news" cutoff and bullish/bearish labels.
"""

import asyncio
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from app import archive as archive_module
from app import window
from app.coins import COIN_NAMES
from app.sentiment_service import compute_sentiment_payloads

# Reuse the live API's own thresholds -- never redefine them here.
MIN_EFFECTIVE_SAMPLE = window.MIN_EFFECTIVE_SAMPLE
INSUFFICIENT_LABEL = window.INSUFFICIENT_LABEL
BULLISH_AT, BEARISH_AT = window.BULLISH_AT, window.BEARISH_AT
WINDOW_VERSION = window.WINDOW_VERSION

MOVERS_PRICE_USD = os.environ.get("X402_MOVERS_PRICE_USD", "$0.02")

# /market-sentiment is free and unauthenticated, so recomputing it live on
# every request (a feed fetch across all 24 coins) would let anyone force
# that work just by hitting the endpoint repeatedly. Cache the aggregate
# for this long and serve the cached value in between -- a stale-by-up-to-
# this-much index is the right trade for a free, high-traffic discovery
# endpoint; the paid /sentiment/{symbol} stays live on every call.
MARKET_SENTIMENT_CACHE_SECONDS = int(os.environ.get("MARKET_SENTIMENT_CACHE_SECONDS", 3600))
MOVERS_LOOKBACK = timedelta(hours=24)
# How far either side of exactly 24h ago an archived row can be and still
# count as "the ~24h-ago reading" -- hourly logging has occasional gaps
# (see /archive), so an exact 24h-ago row won't always exist.
MOVERS_LOOKBACK_TOLERANCE = timedelta(hours=4)
MOVERS_DEFAULT_LIMIT = 10
MOVERS_MAX_LIMIT = 24


def _label_from_score(score: float) -> str:
    if score >= BULLISH_AT:
        return "bullish"
    if score <= BEARISH_AT:
        return "bearish"
    return "neutral"


def compute_market_index(payloads: dict) -> dict:
    """Aggregate several coins' /sentiment payloads (as returned by
    compute_sentiment_payloads) into one market-wide index.

    Only coins with a real label (not INSUFFICIENT_LABEL) and
    effective_sample_size >= MIN_EFFECTIVE_SAMPLE are included, so a
    quiet-news day for thin altcoins doesn't corrupt the index; each
    included coin is weighted by its own effective_sample_size, so a coin
    with 50 fresh headlines moves the index more than one with 6.
    """
    included = []
    for symbol, payload in payloads.items():
        overall = payload.get("overall_sentiment") or {}
        effective_n = overall.get("effective_sample_size") or 0.0
        if overall.get("label") == INSUFFICIENT_LABEL or effective_n < MIN_EFFECTIVE_SAMPLE:
            continue
        included.append((symbol, overall["average_compound"], effective_n))

    if not included:
        return {
            "market_sentiment": None,
            "label": INSUFFICIENT_LABEL,
            "coins_included": 0,
            "coins_considered": len(payloads),
            "contributors": [],
            "window": WINDOW_VERSION,
            "note": "No coin had enough recent coverage to compute a market index.",
        }

    total_weight = sum(n for _, _, n in included)
    weighted_score = sum(score * n for _, score, n in included) / total_weight

    contributors = sorted(
        (
            {
                "symbol": symbol,
                "average_compound": round(score, 4),
                "weight": round(n / total_weight, 4),
            }
            for symbol, score, n in included
        ),
        key=lambda c: c["weight"],
        reverse=True,
    )

    return {
        "market_sentiment": round(weighted_score, 4),
        "label": _label_from_score(weighted_score),
        "coins_included": len(included),
        "coins_considered": len(payloads),
        "contributors": contributors,
        "window": WINDOW_VERSION,
    }


# In-process cache of the live per-coin payloads for every supported coin
# (one compute_sentiment_payloads(COIN_NAMES) call = one feed fetch). Both
# /market-sentiment (free) and the "now" side of /movers (paid) read from
# it, so neither can trigger more than one live 24-coin fetch per
# MARKET_SENTIMENT_CACHE_SECONDS. There's only one cacheable shape (all of
# COIN_NAMES), so a single slot is enough. The lock serializes concurrent
# refreshes so a burst of requests after the cache goes stale triggers at
# most one feed fetch, not one per request.
_market_cache_lock = asyncio.Lock()
_market_cache: dict = {"computed_at": None, "payloads": None}


def _market_cache_is_fresh(now: datetime) -> bool:
    computed_at = _market_cache["computed_at"]
    return computed_at is not None and (now - computed_at).total_seconds() < MARKET_SENTIMENT_CACHE_SECONDS


async def current_payloads(now: Optional[datetime] = None) -> tuple:
    """(payloads, computed_at): the live per-coin payloads for every
    supported coin, refreshed at most once per MARKET_SENTIMENT_CACHE_SECONDS.
    Shared by /market-sentiment and /movers."""
    now = now or datetime.now(timezone.utc)
    async with _market_cache_lock:
        if not _market_cache_is_fresh(now):
            _market_cache["payloads"] = await compute_sentiment_payloads(list(COIN_NAMES))
            _market_cache["computed_at"] = now
        return _market_cache["payloads"], _market_cache["computed_at"]


async def compute_market_sentiment(symbols: Optional[list] = None, now: Optional[datetime] = None) -> dict:
    """/market-sentiment payload, from the shared cache (current_payloads,
    default 1h) when called with the default coin list -- the shape every
    real request uses. A custom `symbols` list (used by tests) always
    recomputes live and never touches the cache.

    `cached`, `computed_at` and `next_refresh_at` are added to the result so
    callers can see exactly how fresh the number is -- this is a free,
    best-effort index, not a claim of a live reading."""
    now = now or datetime.now(timezone.utc)

    if symbols is not None:
        payloads = await compute_sentiment_payloads(list(symbols))
        result = compute_market_index(payloads)
        result.update(cached=False, computed_at=now.isoformat(), next_refresh_at=None)
        return result

    payloads, computed_at = await current_payloads(now)
    next_refresh_at = computed_at + timedelta(seconds=MARKET_SENTIMENT_CACHE_SECONDS)
    return {
        **compute_market_index(payloads),
        "cached": now != computed_at,
        "computed_at": computed_at.isoformat(),
        "next_refresh_at": next_refresh_at.isoformat(),
    }


async def compute_movers_cached(limit: int = MOVERS_DEFAULT_LIMIT, now: Optional[datetime] = None) -> dict:
    """/movers payload. The "now" side comes from the shared hourly cache
    (current_payloads), so a paid call never waits on a live 24-coin feed
    fetch more than once per MARKET_SENTIMENT_CACHE_SECONDS; the ~24h-ago
    side is the archived hourly reading closest to 24h before computed_at.
    computed_at is returned so buyers see how fresh the "now" side is."""
    payloads, computed_at = await current_payloads(now)
    result = compute_movers(payloads, limit=limit, now=computed_at)
    result["computed_at"] = computed_at.isoformat()
    return result


def _closest_row(rows: list, target: datetime, tolerance: timedelta) -> Optional[dict]:
    """The row in `rows` (each with an "observed_at" ISO string) closest to
    `target`, or None if none is within `tolerance`."""
    best, best_delta = None, None
    for row in rows:
        observed = archive_module._parse_ts(row["observed_at"])
        delta = abs(observed - target)
        if delta <= tolerance and (best_delta is None or delta < best_delta):
            best, best_delta = row, delta
    return best


def compute_movers(current: dict, limit: int = MOVERS_DEFAULT_LIMIT, now: Optional[datetime] = None) -> dict:
    """Rank coins by sentiment SHIFT between `current` (live payloads, as
    from compute_sentiment_payloads) and each coin's archived hourly
    reading closest to ~24h ago (app.archive.hourly_rows).

    A coin only appears if both readings clear MIN_EFFECTIVE_SAMPLE, so a
    shift isn't just noise from a coin that had 1 headline yesterday and 2
    today. A coin with no ~24h-ago archive row within tolerance (too new
    to the archive, or a logging gap) is left out rather than guessed at.
    """
    now = now or datetime.now(timezone.utc)
    target = now - MOVERS_LOOKBACK
    limit = max(1, min(limit, MOVERS_MAX_LIMIT))

    rows_by_symbol: dict = {}
    for row in archive_module.hourly_rows():
        rows_by_symbol.setdefault(row["symbol"], []).append(row)

    shifts = []
    for symbol, payload in current.items():
        overall = payload.get("overall_sentiment") or {}
        current_n = overall.get("effective_sample_size") or 0.0
        if overall.get("label") == INSUFFICIENT_LABEL or current_n < MIN_EFFECTIVE_SAMPLE:
            continue

        prev = _closest_row(rows_by_symbol.get(symbol, []), target, MOVERS_LOOKBACK_TOLERANCE)
        if prev is None:
            continue
        prev_score = prev.get("average_compound")
        prev_n = prev.get("effective_sample_size") or 0.0
        if prev_score is None or prev_n < MIN_EFFECTIVE_SAMPLE:
            continue

        shifts.append({
            "symbol": symbol,
            "current_score": round(overall["average_compound"], 4),
            "current_label": overall.get("label"),
            "previous_score": round(prev_score, 4),
            "delta": round(overall["average_compound"] - prev_score, 4),
            "previous_observed_at": prev["observed_at"],
        })

    shifts.sort(key=lambda s: s["delta"], reverse=True)

    return {
        "movers_up": shifts[:limit],
        "movers_down": list(reversed(shifts[-limit:])) if len(shifts) > limit else [],
        "coins_ranked": len(shifts),
        "coins_considered": len(current),
        "lookback_hours": MOVERS_LOOKBACK.total_seconds() / 3600,
        "window": WINDOW_VERSION,
    }


MARKET_SENTIMENT_EXAMPLE = {
    "market_sentiment": 0.1951,
    "label": "bullish",
    "coins_included": 3,
    "coins_considered": 24,
    "contributors": [
        {"symbol": "BTC", "average_compound": 0.2518, "weight": 0.4731},
        {"symbol": "ETH", "average_compound": 0.18, "weight": 0.3165},
        {"symbol": "SOL", "average_compound": -0.05, "weight": 0.2104},
    ],
    "window": WINDOW_VERSION,
    "cached": True,
    "computed_at": "2026-11-09T20:00:00+00:00",
    "next_refresh_at": "2026-11-09T21:00:00+00:00",
}

MOVERS_EXAMPLE = {
    "movers_up": [
        {
            "symbol": "SOL",
            "current_score": 0.42,
            "current_label": "bullish",
            "previous_score": 0.05,
            "delta": 0.37,
            "previous_observed_at": "2026-11-08T14:00:02+00:00",
        },
    ],
    "movers_down": [
        {
            "symbol": "ETH",
            "current_score": -0.1,
            "current_label": "bearish",
            "previous_score": 0.25,
            "delta": -0.35,
            "previous_observed_at": "2026-11-08T14:00:02+00:00",
        },
    ],
    "coins_ranked": 18,
    "coins_considered": 24,
    "lookback_hours": 24.0,
    "window": WINDOW_VERSION,
    "computed_at": "2026-11-09T20:00:00+00:00",
}
