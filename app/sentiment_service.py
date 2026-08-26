"""
Shared sentiment-computation logic. Both the HTTP endpoints (app/main.py --
x402 and subscription lanes) and the background alert poller (app/alerts.py)
call this so there is exactly one code path computing a score.
"""

import asyncio

from app.sources.reddit import fetch_reddit_posts
from app.sources.news import fetch_news_headlines
from app.sources.feargreed import fetch_fear_greed
from app.sentiment import score_texts
from app.coins import resolve_name, validate_symbol


async def compute_sentiment_payload(symbol: str) -> dict:
    """Raises ValueError on an invalid symbol."""
    symbol = validate_symbol(symbol)
    name = resolve_name(symbol)

    reddit_task = fetch_reddit_posts(symbol)
    news_task = fetch_news_headlines(symbol, name)
    fng_task = fetch_fear_greed()

    reddit_texts, news_texts, fear_greed = await asyncio.gather(
        reddit_task, news_task, fng_task
    )

    all_texts = reddit_texts + news_texts
    overall = score_texts(all_texts)
    reddit_score = score_texts(reddit_texts)
    news_score = score_texts(news_texts)

    return {
        "symbol": symbol,
        "name": name,
        "overall_sentiment": overall,
        "breakdown": {
            "reddit": reddit_score,
            "news": news_score,
            "fear_greed_index": fear_greed,
        },
        "sources": [
            "reddit.com (r/CryptoCurrency, r/Bitcoin, r/CryptoMarkets)",
            "coindesk.com RSS",
            "cointelegraph.com RSS",
            "decrypt.co RSS",
            "alternative.me Fear & Greed Index",
        ],
    }
