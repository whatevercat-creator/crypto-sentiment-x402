"""
Free crypto news source via public RSS feeds (no API key required).
"""

import asyncio

import httpx
import xml.etree.ElementTree as ET
from typing import List

from app.coins import mentions

NEWS_OUTLETS = {
    "CoinDesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "Cointelegraph": "https://cointelegraph.com/rss",
    "Decrypt": "https://decrypt.co/feed",
    "Bitcoin Magazine": "https://bitcoinmagazine.com/.rss/full/",
    "The Block": "https://www.theblock.co/rss.xml",
    "CryptoSlate": "https://cryptoslate.com/feed/",
    "NewsBTC": "https://www.newsbtc.com/feed/",
    "CryptoPotato": "https://cryptopotato.com/feed/",
    "The Defiant": "https://thedefiant.io/feed",
    "DL News": "https://www.dlnews.com/arc/outboundfeeds/rss/",
}
RSS_FEEDS = list(NEWS_OUTLETS.values())


# One retry for a 5xx or timeout: DL News' feed answers 504 now and then
# (seen 2026-10-03) and serves fine on the next request.
RETRY_DELAY_SECONDS = 1.0


async def _get_feed(client: httpx.AsyncClient, feed_url: str) -> httpx.Response:
    for attempt in (1, 2):
        try:
            resp = await client.get(feed_url)
        except httpx.TimeoutException:
            if attempt == 2:
                raise
        else:
            if resp.status_code < 500 or attempt == 2:
                resp.raise_for_status()
                return resp
        await asyncio.sleep(RETRY_DELAY_SECONDS)
    raise AssertionError("unreachable")


async def fetch_feed_items(limit_per_feed: int = 30) -> List[str]:
    """Fetch every RSS feed once and return "title description" for up to
    `limit_per_feed` items per feed. A feed that fails is skipped."""
    items: List[str] = []
    async with httpx.AsyncClient(
        timeout=10,
        headers={"User-Agent": "crypto-sentiment-x402/1.0"},
        follow_redirects=True,
    ) as client:
        for feed_url in RSS_FEEDS:
            try:
                resp = await _get_feed(client, feed_url)
                root = ET.fromstring(resp.content)
            except (httpx.HTTPError, ET.ParseError):
                continue

            for item in root.findall(".//item")[:limit_per_feed]:
                title = (item.findtext("title") or "").strip()
                desc = (item.findtext("description") or "").strip()
                items.append(f"{title} {desc}")
    return items


def select_headlines(items: List[str], symbol: str) -> List[str]:
    """The items that mention `symbol` (whole-word, see app/coins.py)."""
    return [text[:500] for text in items if mentions(symbol, text)]


async def fetch_news_headlines(symbol: str, name: str = "", limit_per_feed: int = 30) -> List[str]:
    """
    Pull recent headlines/descriptions from crypto news RSS feeds that
    mention the coin's ticker, name or aliases as whole words. `name` is
    kept for callers that pass it; matching uses app/coins.py's terms.
    """
    return select_headlines(await fetch_feed_items(limit_per_feed), symbol)
