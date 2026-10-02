"""
Free crypto news source via public RSS feeds (no API key required).
"""

import httpx
import xml.etree.ElementTree as ET
from typing import List

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


async def fetch_news_headlines(symbol: str, name: str, limit_per_feed: int = 30) -> List[str]:
    """
    Pull recent headlines/descriptions from crypto news RSS feeds that
    mention the coin's symbol or full name.
    """
    texts: List[str] = []
    needles = {symbol.lower(), name.lower()}

    async with httpx.AsyncClient(
        timeout=10,
        headers={"User-Agent": "crypto-sentiment-x402/1.0"},
        follow_redirects=True,
    ) as client:
        for feed_url in RSS_FEEDS:
            try:
                resp = await client.get(feed_url)
                resp.raise_for_status()
                root = ET.fromstring(resp.content)
            except (httpx.HTTPError, ET.ParseError):
                continue

            items = root.findall(".//item")[:limit_per_feed]
            for item in items:
                title = (item.findtext("title") or "").strip()
                desc = (item.findtext("description") or "").strip()
                combined = f"{title} {desc}"
                if any(n in combined.lower() for n in needles):
                    texts.append(combined[:500])

    return texts
