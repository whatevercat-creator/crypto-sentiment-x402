"""
Historical sentiment dataset, sold as a growing export via the existing
Stripe billing (see app/billing.py TIERS["data"]).

There is no backfilled history -- this only has data from whenever the
background snapshot loop below first ran. That's disclosed to buyers
(see /dataset/info) rather than faked.

A background asyncio loop (started in app/main.py's startup event, same
pattern as app/alerts.py) takes one sentiment reading per tracked symbol
per calendar day and stores it. GET /dataset/export lets a customer with
dataset access (the "data" tier, or any future tier with
TIERS[tier]["dataset_access"] = True) download everything collected so
far as CSV or JSON.

Env vars (see .env.example):
  SNAPSHOT_SYMBOLS           - comma-separated symbols to track (default:
                                every symbol in app/coins.py's COIN_NAMES)
  SNAPSHOT_INTERVAL_SECONDS  - how often the loop wakes up to check whether
                                today's snapshot is still needed (default
                                3600 = hourly; it still only records ONE
                                row per symbol per calendar day)

CAVEAT: like the alert poller, this only works correctly with a single
running instance -- see ALERTS.md's caveat, same reasoning applies here.
"""

import csv
import io
import os
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Header, Query
from fastapi.responses import PlainTextResponse, JSONResponse

from app.billing import _db, TIERS, get_key_info
from app.coins import COIN_NAMES
from app.sentiment_service import compute_sentiment_payload

router = APIRouter(prefix="/dataset", tags=["dataset"])

SNAPSHOT_SYMBOLS = [
    s.strip().upper()
    for s in os.environ.get("SNAPSHOT_SYMBOLS", ",".join(COIN_NAMES.keys())).split(",")
    if s.strip()
]
SNAPSHOT_INTERVAL_SECONDS = int(os.environ.get("SNAPSHOT_INTERVAL_SECONDS", "3600"))


def init_dataset_db() -> None:
    with _db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sentiment_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                snapshot_date TEXT NOT NULL,
                label TEXT NOT NULL,
                average_compound REAL NOT NULL,
                sample_size INTEGER,
                fear_greed_value INTEGER,
                created_at TEXT NOT NULL,
                UNIQUE(symbol, snapshot_date)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_snapshots_symbol_date "
            "ON sentiment_snapshots (symbol, snapshot_date)"
        )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


async def _snapshot_symbol_if_needed(symbol: str) -> None:
    today = _today()
    with _db() as conn:
        row = conn.execute(
            "SELECT 1 FROM sentiment_snapshots WHERE symbol = ? AND snapshot_date = ?",
            (symbol, today),
        ).fetchone()
    if row:
        return  # already snapshotted today

    try:
        payload = await compute_sentiment_payload(symbol)
    except Exception:
        return  # source hiccup -- next loop iteration will retry

    overall = payload["overall_sentiment"]
    fng = payload.get("breakdown", {}).get("fear_greed_index") or {}
    with _db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO sentiment_snapshots "
            "(symbol, snapshot_date, label, average_compound, sample_size, "
            "fear_greed_value, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                symbol,
                today,
                overall.get("label"),
                overall.get("average_compound"),
                overall.get("sample_size"),
                fng.get("value"),
                _now_iso(),
            ),
        )


async def snapshot_loop() -> None:
    import asyncio

    while True:
        for symbol in SNAPSHOT_SYMBOLS:
            await _snapshot_symbol_if_needed(symbol)
        await asyncio.sleep(SNAPSHOT_INTERVAL_SECONDS)


def _require_dataset_access(api_key: str) -> dict:
    info = get_key_info(api_key)
    if not TIERS.get(info["tier"], {}).get("dataset_access"):
        raise HTTPException(
            403,
            "Your tier doesn't include dataset access. Get it with "
            "POST /billing/checkout/data.",
        )
    return info


@router.get("/info")
def dataset_info():
    with _db() as conn:
        row = conn.execute(
            "SELECT MIN(snapshot_date) AS first_date, MAX(snapshot_date) AS last_date, "
            "COUNT(*) AS total_rows FROM sentiment_snapshots"
        ).fetchone()
    return {
        "symbols_tracked": SNAPSHOT_SYMBOLS,
        "first_snapshot_date": row["first_date"],
        "last_snapshot_date": row["last_date"],
        "total_rows": row["total_rows"] or 0,
        "note": "One row per symbol per calendar day, collected going forward "
        "from first_snapshot_date -- there is no backfilled history before that.",
        "get_access": "POST /billing/checkout/data",
    }


@router.get("/export")
def dataset_export(
    x_api_key: str = Header(..., alias="X-API-Key"),
    format: str = Query("csv", pattern="^(csv|json)$"),
    symbol: str | None = Query(None, description="Filter to one symbol, e.g. BTC"),
    since: str | None = Query(None, description="Only rows on/after this date, YYYY-MM-DD"),
):
    _require_dataset_access(x_api_key)

    query = "SELECT symbol, snapshot_date, label, average_compound, sample_size, fear_greed_value FROM sentiment_snapshots WHERE 1=1"
    params: list = []
    if symbol:
        query += " AND symbol = ?"
        params.append(symbol.upper().strip())
    if since:
        query += " AND snapshot_date >= ?"
        params.append(since)
    query += " ORDER BY snapshot_date ASC, symbol ASC"

    with _db() as conn:
        rows = [dict(r) for r in conn.execute(query, params).fetchall()]

    if format == "json":
        return JSONResponse({"rows": rows, "count": len(rows)})

    buf = io.StringIO()
    writer = csv.DictWriter(
        buf,
        fieldnames=["symbol", "snapshot_date", "label", "average_compound", "sample_size", "fear_greed_value"],
    )
    writer.writeheader()
    writer.writerows(rows)
    return PlainTextResponse(
        buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=crypto_sentiment_history.csv"},
    )
