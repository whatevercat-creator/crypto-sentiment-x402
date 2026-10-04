"""
Historical sentiment dataset, sold as a growing export via the existing
Stripe billing (see TIERS[*]["dataset_access"] in app/billing.py).

There is no backfilled history -- this only has data from whenever the
background snapshot loop below first ran. That's disclosed to buyers
(see /dataset/info) rather than faked.

A background asyncio loop (started in app/main.py's startup event, same
pattern as app/alerts.py) takes one sentiment reading per tracked symbol
per calendar day and stores it. GET /dataset/export lets a customer with
dataset access (any tier with TIERS[tier]["dataset_access"] = True --
currently Pro and Data Access) download everything collected so
far as CSV or JSON.

Env vars (see .env.example):
  SNAPSHOT_SYMBOLS           - comma-separated symbols to track (default:
                                app/coins.py's ORIGINAL_SYMBOLS, the 15
                                coins tracked before more names were added)
  SNAPSHOT_INTERVAL_SECONDS  - how often the loop wakes up to check whether
                                today's snapshot is still needed (default
                                3600 = hourly; it still only records ONE
                                row per symbol per calendar day)

CAVEAT: like the alert poller, this only works correctly with a single
running instance -- see ALERTS.md's caveat, same reasoning applies here.
"""

import csv
import io
import json
import os
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Header, Query
from fastapi.responses import PlainTextResponse, JSONResponse

from app.billing import API_KEY_SECURITY, _db, TIERS, dataset_plans, get_key_info
from app.coins import ORIGINAL_SYMBOLS
from app.sentiment_service import compute_sentiment_payload

router = APIRouter(prefix="/dataset", tags=["dataset"])

SNAPSHOT_SYMBOLS = [
    s.strip().upper()
    for s in os.environ.get("SNAPSHOT_SYMBOLS", ",".join(ORIGINAL_SYMBOLS)).split(",")
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


def _access_checkouts() -> str:
    """e.g. "POST /billing/checkout/pro or POST /billing/checkout/data"."""
    return " or ".join(f"POST /billing/checkout/{tier}" for tier in dataset_plans())


def _require_dataset_access(api_key: str) -> dict:
    info = get_key_info(api_key)
    if not TIERS.get(info["tier"], {}).get("dataset_access"):
        raise HTTPException(
            403,
            f"Your tier doesn't include dataset access. Get it with "
            f"{_access_checkouts()}.",
        )
    return info


@router.get("/info", openapi_extra={"security": []})
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
        "hourly": "The export also includes the hourly archive (one reading per logged "
        "symbol per UTC hour, recorded live, never backfilled): `hourly_rows` in "
        "format=json, or format=csv&table=hourly. Coverage: GET /archive.",
        "get_access": _access_checkouts(),
        # Same plan entries /billing/pricing returns, filtered to the
        # tiers that unlock /dataset/export.
        "unlocked_by": [
            {
                "plan": tier,
                "label": plan["label"],
                "price_usd_per_month": plan["price_usd_per_month"],
                "includes": plan["includes"],
                "checkout": f"POST /billing/checkout/{tier}",
            }
            for tier, plan in dataset_plans().items()
        ],
    }


HOURLY_CSV_FIELDS = [
    "symbol", "observed_at", "average_compound", "sample_size", "fear_greed_value", "matcher", "drivers",
    "unweighted_compound_72h", "effective_sample_size", "newest_headline_age_hours", "window",
]


def _hourly_export_rows(symbol: str | None, since: str | None) -> list:
    from app.archive import _parse_ts, hourly_rows

    start = _parse_ts(since) if since else None
    rows = hourly_rows(symbol.upper().strip() if symbol else None, start=start)
    if symbol:
        rows = [{"symbol": symbol.upper().strip(), **r} for r in rows]
    return rows


@router.get("/export", openapi_extra=API_KEY_SECURITY)
def dataset_export(
    x_api_key: str = Header(..., alias="X-API-Key"),
    format: str = Query("csv", pattern="^(csv|json)$"),
    symbol: str | None = Query(None, description="Filter to one symbol, e.g. BTC"),
    since: str | None = Query(None, description="Only rows on/after this date, YYYY-MM-DD"),
    table: str = Query(
        "daily",
        pattern="^(daily|hourly)$",
        description="CSV only: which table to download. JSON always includes both "
        "(`rows` daily, `hourly_rows` hourly).",
    ),
):
    """Daily snapshots plus the hourly archive (see /archive). Hourly rows
    carry matcher and drivers (top 3 headlines as JSON; null before
    2026-10-03)."""
    _require_dataset_access(x_api_key)
    if since:
        try:
            datetime.fromisoformat(since)
        except ValueError:
            raise HTTPException(400, "since must be a date like 2026-10-01")

    if table == "hourly" and format == "csv":
        hourly = _hourly_export_rows(symbol, since)
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=HOURLY_CSV_FIELDS)
        writer.writeheader()
        for row in hourly:
            writer.writerow({**row, "drivers": "" if row["drivers"] is None else json.dumps(row["drivers"])})
        return PlainTextResponse(
            buf.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=crypto_sentiment_hourly.csv"},
        )

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
        hourly = _hourly_export_rows(symbol, since)
        return JSONResponse({
            "rows": rows,
            "count": len(rows),
            "hourly_rows": hourly,
            "hourly_count": len(hourly),
        })

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
