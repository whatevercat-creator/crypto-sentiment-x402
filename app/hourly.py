"""
Hourly sentiment log: used for the lead/lag analysis against price and sold
as the hourly archive (GET /archive describes it, GET /history/{symbol}
sells it per call, and /dataset/export includes it -- see app/archive.py).

Separate from the daily dataset in app/dataset.py: its own table and its
own loop.

A background asyncio loop (started in app/main.py's startup event) wakes at
the top of every UTC hour and stores one reading per symbol, stamped with the
exact UTC time the reading finished. If a symbol already has a row for the
current hour (e.g. after a mid-hour restart) it is skipped, and if the
sources fail that hour is left as a gap rather than filled in. All symbols
are scored from one fetch of the feeds per hour, and each row records the
headline matcher that produced it (`matcher`; NULL for substring-era rows
from before 2026-10-03) and, as JSON, the top 3 headlines that drove it
(`drivers`; NULL for earlier rows).

What average_compound holds depends on the row's `window` column:
- window = "72h-hl24" (2026-10-04 on): the recency-weighted average over
  headlines from the last 72 hours (weight halves every 24 hours) -- the
  same number the API returns as average_compound. Alongside it:
  unweighted_compound_72h, effective_sample_size, newest_headline_age_hours.
- window NULL (earlier rows): the plain average over every item the feeds
  held, however old. The four new columns are NULL on those rows.

Each hour the logger also stores every fetched headline that matches any
supported coin in sentiment_headlines (one row per link: title, source,
link, published, first_seen, its own score, the symbols it matched). Titles
and links only, never article text, so future method changes can be
recomputed from stored data.

Note: the Fear & Greed Index only updates once a day, so fear_greed_value is
constant within a day here -- only average_compound moves hourly.

Env vars (see .env.example):
  HOURLY_ENABLED  - "0"/"false" disables the loop (default on)
  HOURLY_SYMBOLS  - comma-separated symbols to log (default "BTC,ETH")

CAVEAT: same single-instance assumption as ALERTS.md / DATASET.md.
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone

from app import window
from app.billing import _db
from app.coins import COIN_NAMES, MATCHER_VERSION, mentions
from app.sentiment_service import fetch_inputs, payloads_from

logger = logging.getLogger("app.hourly")

HOURLY_ENABLED = os.environ.get("HOURLY_ENABLED", "1").strip().lower() not in ("0", "false", "no", "")
HOURLY_SYMBOLS = [
    s.strip().upper()
    for s in os.environ.get("HOURLY_SYMBOLS", "BTC,ETH").split(",")
    if s.strip()
]


# Headlines stored with each hourly row (the response carries up to 5).
HOURLY_DRIVERS = 3


_WINDOW_COLUMNS = (
    ("unweighted_compound_72h", "REAL"),
    ("effective_sample_size", "REAL"),
    ("newest_headline_age_hours", "REAL"),
    ("window", "TEXT"),
)


def store_headlines(items: list, seen_at: str) -> int:
    """Upsert every item that matches at least one coin in COIN_NAMES into
    sentiment_headlines, deduped by link (items without a link are skipped:
    nothing to dedupe on). A headline seen again keeps its first_seen and
    gains any newly matched symbols. Returns how many rows were new."""
    new = 0
    with _db() as conn:
        for h in items:
            if not h.link:
                continue
            symbols = sorted(s for s in COIN_NAMES if mentions(s, h.text))
            if not symbols:
                continue
            row = conn.execute("SELECT symbols FROM sentiment_headlines WHERE link = ?", (h.link,)).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO sentiment_headlines (link, title, source, published, first_seen, score, symbols) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (h.link, h.title, h.source, h.published, seen_at,
                     window.score_text(h.text[:window.SCORED_CHARS]), json.dumps(symbols)),
                )
                new += 1
            else:
                merged = sorted(set(json.loads(row[0])) | set(symbols))
                if merged != json.loads(row[0]):
                    conn.execute("UPDATE sentiment_headlines SET symbols = ? WHERE link = ?", (json.dumps(merged), h.link))
    return new


def init_hourly_db() -> None:
    with _db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sentiment_hourly (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                average_compound REAL,
                sample_size INTEGER,
                fear_greed_value INTEGER
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_hourly_symbol_time "
            "ON sentiment_hourly (symbol, observed_at)"
        )
        # Which headline matcher produced the reading. NULL = a row from
        # before 2026-10-03, scored with substring matching.
        columns = {row[1] for row in conn.execute("PRAGMA table_info(sentiment_hourly)")}
        if "matcher" not in columns:
            conn.execute("ALTER TABLE sentiment_hourly ADD COLUMN matcher TEXT")
        # Top HOURLY_DRIVERS headlines behind the reading, as JSON (title,
        # source, link, published, score). NULL for rows from before
        # 2026-10-03, which didn't record them.
        if "drivers" not in columns:
            conn.execute("ALTER TABLE sentiment_hourly ADD COLUMN drivers TEXT")
        # From 2026-10-04 (app/window.py); NULL on earlier rows. "window" is
        # an SQL keyword, so it's always quoted in queries.
        for column, kind in _WINDOW_COLUMNS:
            if column not in columns:
                conn.execute(f'ALTER TABLE sentiment_hourly ADD COLUMN "{column}" {kind}')

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sentiment_headlines (
                link TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                source TEXT NOT NULL,
                published TEXT,
                first_seen TEXT NOT NULL,
                score REAL NOT NULL,
                symbols TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_headlines_published ON sentiment_headlines (published)"
        )


def _already_logged(symbol: str, hour_start: datetime) -> bool:
    hour_end = hour_start + timedelta(hours=1)
    with _db() as conn:
        row = conn.execute(
            "SELECT 1 FROM sentiment_hourly WHERE symbol = ? "
            "AND observed_at >= ? AND observed_at < ?",
            (symbol, hour_start.isoformat(), hour_end.isoformat()),
        ).fetchone()
    return row is not None


async def log_hour(symbols: list) -> None:
    """One reading per symbol for the current hour, all scored from a single
    fetch of the feeds (compute_sentiment_payloads), so every symbol is
    stamped with the same observed_at and adding symbols adds no requests."""
    hour_start = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    pending = []
    for symbol in symbols:
        if _already_logged(symbol, hour_start):
            logger.info("[hourly] %s: already logged for %s, skipping", symbol, hour_start.strftime("%Y-%m-%d %H:00Z"))
        else:
            pending.append(symbol)
    if not pending:
        return

    try:
        items, fear_greed = await fetch_inputs()
        now = datetime.now(timezone.utc)
        payloads = payloads_from(items, fear_greed, pending, now)
    except Exception:
        logger.warning("[hourly] %s: sentiment fetch failed, skipping this hour", ",".join(pending), exc_info=True)
        return

    observed_at = now.isoformat()
    try:
        new_headlines = store_headlines(items, observed_at)
        logger.info("[hourly] stored %d new matched headline(s)", new_headlines)
    except Exception:
        logger.warning("[hourly] storing headlines failed; readings are still logged", exc_info=True)

    for symbol, payload in payloads.items():
        overall = payload["overall_sentiment"]
        fng = payload.get("breakdown", {}).get("fear_greed_index") or {}
        compound = overall.get("average_compound")
        sample_size = overall.get("sample_size")
        fng_value = fng.get("value")
        drivers = json.dumps(payload.get("drivers", [])[:HOURLY_DRIVERS])
        with _db() as conn:
            conn.execute(
                "INSERT INTO sentiment_hourly "
                "(symbol, observed_at, average_compound, sample_size, fear_greed_value, matcher, drivers, "
                'unweighted_compound_72h, effective_sample_size, newest_headline_age_hours, "window") '
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (symbol, observed_at, compound, sample_size, fng_value, MATCHER_VERSION, drivers,
                 overall.get("unweighted_compound_72h"), overall.get("effective_sample_size"),
                 overall.get("newest_headline_age_hours"), overall.get("window")),
            )
            total = conn.execute(
                "SELECT COUNT(*) FROM sentiment_hourly WHERE symbol = ?", (symbol,)
            ).fetchone()[0]
        logger.info(
            "[hourly] %s: inserted observed_at=%s compound=%s n=%s fng=%s (total rows for %s: %d)",
            symbol, observed_at, compound, sample_size, fng_value, symbol, total,
        )


def _next_hour_start() -> datetime:
    now = datetime.now(timezone.utc)
    return now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)


async def _sleep_until(target: datetime) -> None:
    """Sleep until the wall clock has actually passed target.

    asyncio.sleep can return a few ms early relative to datetime.now() (it
    uses a monotonic clock). Waking at e.g. 21:59:59.998 made the first
    symbol's "already logged this hour?" check look at the PREVIOUS hour,
    so it skipped and that hour was lost (BTC missed 2026-09-28 22:00Z).
    Loop until the wall clock is past target, plus a small margin.
    """
    while True:
        remaining = (target - datetime.now(timezone.utc)).total_seconds()
        if remaining <= 0:
            return
        await asyncio.sleep(remaining + 0.05)


async def hourly_loop() -> None:
    if not HOURLY_ENABLED:
        logger.info("[hourly] disabled: HOURLY_ENABLED is off")
        return

    logger.info("[hourly] enabled: logging %s at the top of every UTC hour", ",".join(HOURLY_SYMBOLS))
    while True:
        await _sleep_until(_next_hour_start())
        try:
            await log_hour(HOURLY_SYMBOLS)
        except Exception:
            logger.exception("[hourly] unexpected error")
