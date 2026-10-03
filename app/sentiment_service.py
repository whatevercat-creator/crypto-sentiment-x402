"""
Shared sentiment-computation logic. Both the HTTP endpoints (app/main.py --
x402 and subscription lanes) and the background alert poller (app/alerts.py)
call this so there is exactly one code path computing a score.
"""

import asyncio

from app.sources.news import fetch_feed_items, fetch_news_headlines, select_headlines
from app.sources.feargreed import fetch_fear_greed
from app.sentiment import score_text, score_texts
from app.coins import resolve_name, validate_symbol

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
    "dlnews.com RSS",
    "alternative.me Fear & Greed Index",
]


# Characters of title + description that get scored per headline.
SCORED_CHARS = 500
MAX_DRIVERS = 5
# Same cut-offs score_texts uses to count positive/negative headlines.
_POSITIVE, _NEGATIVE = 0.05, -0.05


def top_drivers(headlines: list, limit: int = MAX_DRIVERS) -> list:
    """The headlines that moved the score most: each one shifts the average
    by its own score / sample size, so the largest |score| moved it most.
    Headlines scoring exactly 0 didn't move it and are left out. Titles and
    links only, never the description (article text)."""
    scored = [(score_text(h.text[:SCORED_CHARS]), i, h) for i, h in enumerate(headlines)]
    scored = [entry for entry in scored if entry[0] != 0]
    scored.sort(key=lambda entry: (-abs(entry[0]), entry[1]))
    return [
        {
            "title": h.title,
            "source": h.source,
            "link": h.link,
            "published": h.published,
            "score": round(score, 4),
        }
        for score, _, h in scored[:limit]
    ]


def drivers_summary(symbol: str, drivers: list) -> str:
    """One plain line, e.g. "3 of the top 5 headlines are negative, 2 are positive"."""
    n = len(drivers)
    if n == 0:
        return f"No current headlines about {symbol} moved the score."
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


def build_payload(symbol: str, headlines: list, fear_greed) -> dict:
    overall = score_texts([h.text[:SCORED_CHARS] for h in headlines])
    drivers = top_drivers(headlines)
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


async def compute_sentiment_payloads(symbols: list) -> dict:
    """{symbol: payload} for several symbols from ONE fetch of the feeds
    and the Fear & Greed Index, so the request count doesn't grow with the
    number of symbols. Raises ValueError on an invalid symbol."""
    symbols = [validate_symbol(s) for s in symbols]
    items, fear_greed = await asyncio.gather(fetch_feed_items(), fetch_fear_greed())
    return {s: build_payload(s, select_headlines(items, s), fear_greed) for s in symbols}
