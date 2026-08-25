"""
Free Reddit source.

Uses Reddit's public read-only JSON endpoints (no API key required for
low-volume, read-only access). If you hit rate limits in production,
swap this for a free "script" app + PRAW (still free, just needs
registration at https://www.reddit.com/prefs/apps).
"""

import httpx
from typing import List

USER_AGENT = "crypto-sentiment-x402/1.0 (by u/your_reddit_username)"

SUBREDDITS = ["CryptoCurrency", "Bitcoin", "CryptoMarkets"]


async def fetch_reddit_posts(symbol: str, limit_per_sub: int = 15) -> List[str]:
    """
    Fetch recent post titles + selftext mentioning `symbol` from a small
    set of crypto subreddits. Returns a flat list of text snippets.
    """
    texts: List[str] = []
    symbol_lower = symbol.lower()

    async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT}, timeout=10) as client:
        for sub in SUBREDDITS:
            url = f"https://www.reddit.com/r/{sub}/new.json"
            try:
                resp = await client.get(url, params={"limit": limit_per_sub})
                resp.raise_for_status()
                data = resp.json()
            except (httpx.HTTPError, ValueError):
                # If Reddit blocks/rate-limits us, just skip this source
                # rather than failing the whole request.
                continue

            for child in data.get("data", {}).get("children", []):
                post = child.get("data", {})
                title = post.get("title", "") or ""
                body = post.get("selftext", "") or ""
                combined = f"{title} {body}".strip()
                if not combined:
                    continue
                # Only keep posts that actually mention the symbol/coin name
                if symbol_lower in combined.lower():
                    texts.append(combined[:500])  # cap length per post

    return texts
