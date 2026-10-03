"""
Shared sentiment-computation logic. Both the HTTP endpoints (app/main.py --
x402 and subscription lanes) and the background alert poller (app/alerts.py)
call this so there is exactly one code path computing a score.
"""

import asyncio

from app.sources.news import fetch_feed_items, fetch_news_headlines, select_headlines
from app.sources.feargreed import fetch_fear_greed
from app.sentiment import score_texts
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


def build_payload(symbol: str, news_texts: list, fear_greed) -> dict:
    overall = score_texts(news_texts)
    return {
        "symbol": symbol,
        "name": resolve_name(symbol),
        "overall_sentiment": overall,
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

    news_texts, fear_greed = await asyncio.gather(news_task, fng_task)
    return build_payload(symbol, news_texts, fear_greed)


async def compute_sentiment_payloads(symbols: list) -> dict:
    """{symbol: payload} for several symbols from ONE fetch of the feeds
    and the Fear & Greed Index, so the request count doesn't grow with the
    number of symbols. Raises ValueError on an invalid symbol."""
    symbols = [validate_symbol(s) for s in symbols]
    items, fear_greed = await asyncio.gather(fetch_feed_items(), fetch_fear_greed())
    return {s: build_payload(s, select_headlines(items, s), fear_greed) for s in symbols}
