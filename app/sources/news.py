"""
Free crypto news source via public RSS feeds (no API key required).

Feed health: every fetch records, per outlet, when it was checked, how many
items it returned and its newest item's publish time (FEED_HEALTH, shown on
/transparency). An outlet whose newest item is more than STALE_AFTER_HOURS
old is flagged and logged as a warning at most once per hour. Stale outlets
are NOT dropped automatically: the 72-hour window in app/window.py already
keeps their old items out of the score.
"""

import asyncio
import html
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import List, Optional

import httpx
import xml.etree.ElementTree as ET

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
}
# DL News (https://www.dlnews.com/arc/outboundfeeds/rss/) was removed on
# 2026-10-04: its feed kept a current build date but served only items from
# April-May 2026, and those were being scored as current news.
RSS_FEEDS = list(NEWS_OUTLETS.values())
_OUTLET_BY_URL = {url: name for name, url in NEWS_OUTLETS.items()}


@dataclass(frozen=True)
class Headline:
    """One feed item. `text` (title + description) is what gets matched and
    scored; only title, source, link and published are ever returned to
    callers -- never the description, which is article text."""

    title: str
    source: str
    link: Optional[str]
    published: Optional[str]  # ISO 8601 UTC, or None if the feed's date didn't parse
    text: str


_TAG = re.compile(r"<[^>]+>")


def _clean_title(raw: str) -> str:
    return " ".join(html.unescape(_TAG.sub(" ", raw)).split())


def _clean_link(raw: Optional[str]) -> Optional[str]:
    link = (raw or "").strip()
    return link if link.startswith(("https://", "http://")) else None


def _iso_published(raw: Optional[str]) -> Optional[str]:
    try:
        dt = parsedate_to_datetime((raw or "").strip())
    except (TypeError, ValueError, IndexError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


# One retry for a 5xx or timeout: some feeds answer 504 now and then (DL
# News did on 2026-10-03) and serve fine on the next request.
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


logger = logging.getLogger("app.sources.news")

STALE_AFTER_HOURS = 48
WARN_EVERY = timedelta(hours=1)
# outlet -> {"checked_at", "status", "items", "undated_items", "newest_published",
#            "newest_age_hours", "stale", "error"}; in memory, per process.
FEED_HEALTH: dict = {}
_last_warned: dict = {}


def _record_health(outlet: str, now: datetime, items: List["Headline"] = None, error: str = None) -> None:
    newest = None
    for h in items or []:
        dt = _parse_iso(h.published)
        if dt and (newest is None or dt > newest):
            newest = dt
    age = round((now - newest).total_seconds() / 3600, 1) if newest else None
    stale = error is None and (newest is None or age > STALE_AFTER_HOURS)
    FEED_HEALTH[outlet] = {
        "checked_at": now.isoformat(timespec="seconds"),
        "status": "error" if error else ("stale" if stale else "ok"),
        "items": len(items or []),
        "undated_items": sum(1 for h in items or [] if not h.published),
        "newest_published": newest.isoformat().replace("+00:00", "Z") if newest else None,
        "newest_age_hours": age,
        "stale": stale,
        "error": error,
    }
    if stale and now - _last_warned.get(outlet, datetime.min.replace(tzinfo=timezone.utc)) >= WARN_EVERY:
        _last_warned[outlet] = now
        logger.warning(
            "[feeds] %s looks stale: newest item %s (%s hours old, threshold %s); still included, "
            "the 72-hour window keeps old items out of the score",
            outlet, FEED_HEALTH[outlet]["newest_published"] or "has no date", age, STALE_AFTER_HOURS,
        )


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def feed_health() -> dict:
    """Latest health per outlet, for /transparency."""
    stale = sorted(o for o, h in FEED_HEALTH.items() if h["stale"])
    return {
        "stale_after_hours": STALE_AFTER_HOURS,
        "stale_outlets": stale,
        "outlets": {o: FEED_HEALTH.get(o) for o in NEWS_OUTLETS},
        "note": "From the most recent feed fetch by this server (in memory; empty "
        "until the first fetch after a restart). Stale outlets are flagged, not "
        "dropped: the 72-hour window keeps their old items out of the score.",
    }


async def fetch_feed_items(limit_per_feed: int = 30) -> List[Headline]:
    """Fetch every RSS feed once and return up to `limit_per_feed` items per
    feed. A feed that fails is skipped (and recorded in FEED_HEALTH)."""
    items: List[Headline] = []
    async with httpx.AsyncClient(
        timeout=10,
        headers={"User-Agent": "crypto-sentiment-x402/1.0"},
        follow_redirects=True,
    ) as client:
        for feed_url in RSS_FEEDS:
            outlet = _OUTLET_BY_URL.get(feed_url, feed_url)
            try:
                resp = await _get_feed(client, feed_url)
                root = ET.fromstring(resp.content)
            except (httpx.HTTPError, ET.ParseError) as e:
                _record_health(outlet, datetime.now(timezone.utc), error=type(e).__name__)
                continue

            feed_items: List[Headline] = []
            for item in root.findall(".//item")[:limit_per_feed]:
                title = (item.findtext("title") or "").strip()
                desc = (item.findtext("description") or "").strip()
                feed_items.append(
                    Headline(
                        title=_clean_title(title),
                        source=outlet,
                        link=_clean_link(item.findtext("link")),
                        published=_iso_published(item.findtext("pubDate")),
                        text=f"{title} {desc}",
                    )
                )
            _record_health(outlet, datetime.now(timezone.utc), feed_items)
            items.extend(feed_items)
    return items


def select_headlines(items: List[Headline], symbol: str) -> List[Headline]:
    """The items that mention `symbol` (whole-word, see app/coins.py)."""
    return [item for item in items if mentions(symbol, item.text)]


async def fetch_news_headlines(symbol: str, name: str = "", limit_per_feed: int = 30) -> List[Headline]:
    """
    Pull recent headlines/descriptions from crypto news RSS feeds that
    mention the coin's ticker, name or aliases as whole words. `name` is
    kept for callers that pass it; matching uses app/coins.py's terms.
    """
    return select_headlines(await fetch_feed_items(limit_per_feed), symbol)
