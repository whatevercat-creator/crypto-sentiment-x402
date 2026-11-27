"""
Rolling 72-hour story store (method 72h-hl24-v3, STORE_CHANGED_ON in
app/window.py).

Before this, every score used only the items the 9 RSS feeds held at that
moment (up to 30 per feed until 2026-11-27, now 100), so "the last 72
hours" really meant "whatever the feeds still show": on 2026-11-27 the oldest item in any feed was about
52 hours old and most feeds reached back only 9 to 39 hours.

Now every feed fetch (the hourly run and every on-demand call) upserts its
items into the news_items table in the billing database (the same file as
sentiment_hourly), and scoring uses every stored item published in the last
MAX_AGE_HOURS plus the live fetch, deduped. Window, weighting, the
effective-sample-size rule and the drivers output are unchanged
(app/window.py, app/sentiment_service.py).

One row per feed item, deduped by link, or by outlet + title when the item
has no link. Each row holds ONLY: outlet, title, link, published (UTC ISO),
first_seen, the item's score (computed when first seen, on clean_text, the
same way window.in_window scores a live item) and the coins it matches. No
description or other article text is stored. Items with no parseable
publish date are not stored (they never count anyway). A stored item seen
again keeps its first_seen and its score; its coins are re-matched from the
live text, so a matcher change applies to items still in the feeds.

Rows published more than MAX_AGE_HOURS + PURGE_MARGIN_HOURS ago are deleted
on every hourly run (purge_expired, called from app/hourly.py).

If the store can't be read or written, scoring falls back to the live fetch
only (the behavior before 72h-hl24-v3) and one warning line is logged.

CAVEAT: same single-instance assumption as the rest of the billing database.
"""

import dataclasses
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from app import billing, window
from app.coins import COIN_NAMES, mentions

logger = logging.getLogger("app.news_store")

# Tests turn this off (tests/conftest.py) so stored items from one test
# can't leak into another; the store's own tests turn it back on.
ENABLED = True

# Rows are kept this long past the scoring window before they're deleted.
PURGE_MARGIN_HOURS = 1

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS news_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        outlet TEXT NOT NULL,
        title TEXT NOT NULL,
        link TEXT,
        published TEXT NOT NULL,
        first_seen TEXT NOT NULL,
        score REAL NOT NULL,
        coins TEXT NOT NULL
    )
    """,
    # The dedupe key: the link, or outlet + title for an item without one.
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_news_items_key "
    "ON news_items (COALESCE(link, outlet || char(10) || title))",
    "CREATE INDEX IF NOT EXISTS idx_news_items_published ON news_items (published)",
)


def init_news_store(conn=None) -> None:
    """Create the table and indexes if they're missing."""
    own = conn is None
    conn = conn or billing._db()
    try:
        with conn:
            for statement in _SCHEMA:
                conn.execute(statement)
    finally:
        if own:
            conn.close()


def _iso(dt: datetime) -> str:
    """Same form as the feeds' published values: 2026-11-27T21:53:08Z."""
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _cutoff(now: datetime) -> str:
    return _iso(now - timedelta(hours=window.MAX_AGE_HOURS + PURGE_MARGIN_HOURS))


def _key(h) -> tuple:
    return ("link", h.link) if h.link else ("title", h.source, h.title)


def _coins(h) -> List[str]:
    return [s for s in COIN_NAMES if mentions(s, h.text)]


def _normalized_published(h) -> Optional[str]:
    dt = window.parse_published(h.published)
    return _iso(dt) if dt else None


def _upsert(conn, live: list, now: datetime) -> int:
    """Insert live items not stored yet; re-match coins on ones that are.
    Returns how many rows were new."""
    seen_at = now.isoformat(timespec="seconds")
    cutoff = _cutoff(now)
    new = 0
    for h in live:
        published = _normalized_published(h)
        if published is None or published < cutoff:
            continue  # no parseable date, or already past what the store keeps
        coins = json.dumps(_coins(h))
        cur = conn.execute(
            "INSERT OR IGNORE INTO news_items (outlet, title, link, published, first_seen, score, coins) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (h.source, h.title, h.link, published, seen_at,
             window.score_text(h.text[:window.SCORED_CHARS]), coins),
        )
        if cur.rowcount:
            new += 1
        elif h.link:
            conn.execute("UPDATE news_items SET coins = ? WHERE link = ? AND coins != ?", (coins, h.link, coins))
        else:
            conn.execute(
                "UPDATE news_items SET coins = ? WHERE link IS NULL AND outlet = ? AND title = ? AND coins != ?",
                (coins, h.source, h.title, coins),
            )
    return new


def _load(conn, now: datetime) -> list:
    """Stored items published since the purge cutoff, newest first, as
    Headlines carrying their stored score and coins (text is empty: it
    isn't stored)."""
    from app.sources.news import Headline

    rows = conn.execute(
        "SELECT outlet, title, link, published, score, coins FROM news_items "
        "WHERE published >= ? ORDER BY published DESC, id",
        (_cutoff(now),),
    ).fetchall()
    return [
        Headline(title=r[1], source=r[0], link=r[2], published=r[3], text="",
                 score=r[4], coins=tuple(json.loads(r[5])))
        for r in rows
    ]


def with_store(live: list, now: Optional[datetime] = None) -> list:
    """Upsert `live` (one feed fetch's items) and return what gets scored:
    the live items in feed order, each carrying its stored score and coins,
    then the stored items the feeds no longer show, newest first. Deduped
    by link (or outlet + title). Falls back to `live` unchanged, with one
    warning line, if the store can't be read or written."""
    if not ENABLED:
        return live
    now = now or datetime.now(timezone.utc)
    try:
        conn = billing._db()
        try:
            init_news_store(conn)
            with conn:
                _upsert(conn, live, now)
            stored = _load(conn, now)
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001 -- any store failure means live-only, never a failed reading
        logger.warning("[news_store] store unavailable (%s: %s); scoring the live feeds only", type(e).__name__, e)
        return live

    by_key = {}
    for s in stored:
        by_key.setdefault(_key(s), s)
    out, used = [], set()
    for h in live:
        k = _key(h)
        if k in used:
            continue
        s = by_key.get(k)
        if s is None:
            out.append(h)  # undated or too old: window.in_window leaves it out
            continue
        used.add(k)
        out.append(dataclasses.replace(h, score=s.score, coins=s.coins))
    out.extend(s for k, s in by_key.items() if k not in used)
    return out


def purge_expired(now: Optional[datetime] = None) -> int:
    """Delete rows published more than MAX_AGE_HOURS + PURGE_MARGIN_HOURS
    ago. Returns how many were deleted."""
    now = now or datetime.now(timezone.utc)
    conn = billing._db()
    try:
        init_news_store(conn)
        with conn:
            return conn.execute("DELETE FROM news_items WHERE published < ?", (_cutoff(now),)).rowcount
    finally:
        conn.close()
