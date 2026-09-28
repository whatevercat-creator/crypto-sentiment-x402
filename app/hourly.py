"""
Hourly sentiment log for internal lead/lag analysis against price.

Separate from the daily dataset in app/dataset.py: its own table, its own
loop, and it is NOT exposed through /dataset/export. Read it straight from
the sqlite file (BILLING_DB_PATH).

A background asyncio loop (started in app/main.py's startup event) wakes at
the top of every UTC hour and stores one reading per symbol, stamped with the
exact UTC time the reading finished. If a symbol already has a row for the
current hour (e.g. after a mid-hour restart) it is skipped, and if the
sources fail that hour is left as a gap rather than filled in.

Note: the Fear & Greed Index only updates once a day, so fear_greed_value is
constant within a day here -- only average_compound moves hourly.

Env vars (see .env.example):
  HOURLY_ENABLED  - "0"/"false" disables the loop (default on)
  HOURLY_SYMBOLS  - comma-separated symbols to log (default "BTC,ETH")

CAVEAT: same single-instance assumption as ALERTS.md / DATASET.md.
"""

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

from app.billing import _db
from app.sentiment_service import compute_sentiment_payload

logger = logging.getLogger("app.hourly")

HOURLY_ENABLED = os.environ.get("HOURLY_ENABLED", "1").strip().lower() not in ("0", "false", "no", "")
HOURLY_SYMBOLS = [
    s.strip().upper()
    for s in os.environ.get("HOURLY_SYMBOLS", "BTC,ETH").split(",")
    if s.strip()
]


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


async def _log_symbol_if_needed(symbol: str) -> None:
    now = datetime.now(timezone.utc)
    hour_start = now.replace(minute=0, second=0, microsecond=0)
    hour_end = hour_start + timedelta(hours=1)
    with _db() as conn:
        row = conn.execute(
            "SELECT 1 FROM sentiment_hourly WHERE symbol = ? "
            "AND observed_at >= ? AND observed_at < ?",
            (symbol, hour_start.isoformat(), hour_end.isoformat()),
        ).fetchone()
    if row:
        logger.info("[hourly] %s: already logged for %s, skipping", symbol, hour_start.strftime("%Y-%m-%d %H:00Z"))
        return

    try:
        payload = await compute_sentiment_payload(symbol)
    except Exception:
        logger.warning("[hourly] %s: sentiment fetch failed, skipping this hour", symbol, exc_info=True)
        return

    overall = payload["overall_sentiment"]
    fng = payload.get("breakdown", {}).get("fear_greed_index") or {}
    observed_at = datetime.now(timezone.utc).isoformat()
    compound = overall.get("average_compound")
    sample_size = overall.get("sample_size")
    fng_value = fng.get("value")
    with _db() as conn:
        conn.execute(
            "INSERT INTO sentiment_hourly "
            "(symbol, observed_at, average_compound, sample_size, fear_greed_value) "
            "VALUES (?, ?, ?, ?, ?)",
            (symbol, observed_at, compound, sample_size, fng_value),
        )
        total = conn.execute(
            "SELECT COUNT(*) FROM sentiment_hourly WHERE symbol = ?", (symbol,)
        ).fetchone()[0]
    logger.info(
        "[hourly] %s: inserted observed_at=%s compound=%s n=%s fng=%s (total rows for %s: %d)",
        symbol, observed_at, compound, sample_size, fng_value, symbol, total,
    )


def _seconds_until_next_hour() -> float:
    now = datetime.now(timezone.utc)
    next_hour = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    return (next_hour - now).total_seconds()


async def hourly_loop() -> None:
    if not HOURLY_ENABLED:
        logger.info("[hourly] disabled: HOURLY_ENABLED is off")
        return

    logger.info("[hourly] enabled: logging %s at the top of every UTC hour", ",".join(HOURLY_SYMBOLS))
    while True:
        await asyncio.sleep(_seconds_until_next_hour())
        for symbol in HOURLY_SYMBOLS:
            try:
                await _log_symbol_if_needed(symbol)
            except Exception:
                logger.exception("[hourly] %s: unexpected error", symbol)
