"""
Sentiment-shift alerts: watch a symbol, get pinged (webhook/Discord/Telegram)
when its sentiment moves. Bundled into the existing Stripe tiers rather than
sold separately -- see TIERS["*"]["alert_limit"] in app/billing.py.

  free     0 watches  -- no alerts
  starter  3 watches
  pro      15 watches

Endpoints (all require the X-API-Key header used by /v1/sentiment):
  POST   /alerts/watch          {symbol, channel_type, channel_target, threshold?}
  GET    /alerts/watch          list this key's watches
  DELETE /alerts/watch/{id}     stop a watch
  GET    /alerts/history        recent deliveries (debugging aid)

A background asyncio loop (started in app/main.py's startup event) polls
every watched symbol on an interval and fires a notification when the
sentiment label changes or the compound score moves by >= threshold since
the last check.

Env vars (see .env.example):
  ALERT_POLL_INTERVAL_SECONDS  - how often to re-check watched symbols (default 900 = 15 min)
  ALERT_THRESHOLD_DEFAULT      - default |compound score| delta that counts as a "shift" (default 0.3)
  TELEGRAM_BOT_TOKEN           - only needed if any watch uses channel_type "telegram"

CAVEAT: this loop runs in-process, so it only works correctly with a single
running instance of this app. If you scale to multiple instances, either
pin the poller to one of them or move it to a real scheduler/queue.
"""

import asyncio
import os
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, HTTPException, Header
from pydantic import BaseModel, field_validator

from app.billing import _db, TIERS, get_key_info
from app.coins import validate_symbol
from app.sentiment_service import compute_sentiment_payload

router = APIRouter(prefix="/alerts", tags=["alerts"])

POLL_INTERVAL_SECONDS = int(os.environ.get("ALERT_POLL_INTERVAL_SECONDS", "900"))
DEFAULT_THRESHOLD = float(os.environ.get("ALERT_THRESHOLD_DEFAULT", "0.3"))
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")

VALID_CHANNELS = {"webhook", "discord", "telegram"}


def init_alerts_db() -> None:
    with _db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS watches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                api_key TEXT NOT NULL,
                symbol TEXT NOT NULL,
                channel_type TEXT NOT NULL,
                channel_target TEXT NOT NULL,
                threshold REAL NOT NULL DEFAULT 0.3,
                last_label TEXT,
                last_compound REAL,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS alert_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                watch_id INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                old_label TEXT,
                new_label TEXT,
                old_compound REAL,
                new_compound REAL,
                delivered INTEGER NOT NULL DEFAULT 0,
                error TEXT,
                sent_at TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_watches_api_key ON watches (api_key)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_watches_status ON watches (status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_watches_symbol ON watches (symbol)")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class WatchCreate(BaseModel):
    symbol: str
    channel_type: str
    channel_target: str
    threshold: float = DEFAULT_THRESHOLD

    @field_validator("channel_type")
    @classmethod
    def _check_channel(cls, v: str) -> str:
        if v not in VALID_CHANNELS:
            raise ValueError(f"channel_type must be one of {sorted(VALID_CHANNELS)}")
        return v


@router.post("/watch")
def create_watch(body: WatchCreate, x_api_key: str = Header(..., alias="X-API-Key")):
    key_info = get_key_info(x_api_key)
    tier = key_info["tier"]
    alert_limit = TIERS[tier]["alert_limit"]

    if alert_limit == 0:
        raise HTTPException(
            403,
            f"The '{tier}' tier doesn't include alerts. Upgrade with "
            f"POST /billing/checkout/starter or /billing/checkout/pro.",
        )

    if body.channel_type == "telegram" and not TELEGRAM_BOT_TOKEN:
        raise HTTPException(
            503,
            "Telegram alerts aren't configured on this deployment (missing "
            "TELEGRAM_BOT_TOKEN) -- use 'webhook' or 'discord' instead.",
        )

    try:
        symbol = validate_symbol(body.symbol)
    except ValueError as e:
        raise HTTPException(400, str(e))

    with _db() as conn:
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM watches WHERE api_key = ? AND status = 'active'",
            (x_api_key,),
        ).fetchone()["n"]
        if count >= alert_limit:
            raise HTTPException(
                403,
                f"The '{tier}' tier allows up to {alert_limit} active watches "
                f"(you have {count}). Remove one first, or upgrade.",
            )

        cur = conn.execute(
            "INSERT INTO watches "
            "(api_key, symbol, channel_type, channel_target, threshold, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'active', ?)",
            (x_api_key, symbol, body.channel_type, body.channel_target, body.threshold, _now_iso()),
        )
        watch_id = cur.lastrowid

    return {
        "id": watch_id,
        "symbol": symbol,
        "channel_type": body.channel_type,
        "threshold": body.threshold,
        "status": "active",
        "note": f"Checked roughly every {max(POLL_INTERVAL_SECONDS // 60, 1)} minute(s). "
        f"The first check just records a baseline -- no alert fires until the next one after that.",
    }


@router.get("/watch")
def list_watches(x_api_key: str = Header(..., alias="X-API-Key")):
    get_key_info(x_api_key)
    with _db() as conn:
        rows = conn.execute(
            "SELECT id, symbol, channel_type, threshold, last_label, last_compound, status, created_at "
            "FROM watches WHERE api_key = ? ORDER BY created_at DESC",
            (x_api_key,),
        ).fetchall()
    return {"watches": [dict(r) for r in rows]}


@router.delete("/watch/{watch_id}")
def delete_watch(watch_id: int, x_api_key: str = Header(..., alias="X-API-Key")):
    get_key_info(x_api_key)
    with _db() as conn:
        row = conn.execute(
            "SELECT id FROM watches WHERE id = ? AND api_key = ?", (watch_id, x_api_key)
        ).fetchone()
        if not row:
            raise HTTPException(404, "No watch with that id for this API key.")
        conn.execute("UPDATE watches SET status = 'canceled' WHERE id = ?", (watch_id,))
    return {"status": "canceled", "id": watch_id}


@router.get("/history")
def alert_history(x_api_key: str = Header(..., alias="X-API-Key"), limit: int = 20):
    get_key_info(x_api_key)
    limit = max(1, min(limit, 100))
    with _db() as conn:
        rows = conn.execute(
            """
            SELECT ae.* FROM alert_events ae
            JOIN watches w ON w.id = ae.watch_id
            WHERE w.api_key = ?
            ORDER BY ae.sent_at DESC
            LIMIT ?
            """,
            (x_api_key, limit),
        ).fetchall()
    return {"events": [dict(r) for r in rows]}


def _format_message(symbol: str, old_label, new_label, old_compound, new_compound) -> str:
    old_c = f"{old_compound:.2f}" if old_compound is not None else "n/a"
    return (
        f"\U0001F514 {symbol} sentiment shifted: {old_label or 'n/a'} ({old_c}) "
        f"-> {new_label} ({new_compound:.2f})"
    )


async def _deliver(watch: dict, message: str, event_payload: dict) -> tuple[bool, str]:
    channel_type = watch["channel_type"]
    target = watch["channel_target"]
    try:
        async with httpx.AsyncClient(timeout=10) as http:
            if channel_type == "webhook":
                resp = await http.post(target, json=event_payload)
            elif channel_type == "discord":
                resp = await http.post(target, json={"content": message})
            elif channel_type == "telegram":
                resp = await http.post(
                    f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                    json={"chat_id": target, "text": message},
                )
            else:
                return False, f"unknown channel_type {channel_type!r}"
            resp.raise_for_status()
        return True, ""
    except Exception as e:
        return False, str(e)[:500]


async def _poll_once() -> None:
    with _db() as conn:
        symbol_rows = conn.execute(
            "SELECT DISTINCT symbol FROM watches WHERE status = 'active'"
        ).fetchall()
    symbols = [r["symbol"] for r in symbol_rows]

    for symbol in symbols:
        try:
            payload = await compute_sentiment_payload(symbol)
        except Exception:
            continue  # source hiccup this round -- try again next poll

        new_label = payload["overall_sentiment"]["label"]
        new_compound = payload["overall_sentiment"]["average_compound"]

        with _db() as conn:
            watches = conn.execute(
                "SELECT * FROM watches WHERE symbol = ? AND status = 'active'",
                (symbol,),
            ).fetchall()

        for w in watches:
            old_label = w["last_label"]
            old_compound = w["last_compound"]
            threshold = w["threshold"] or DEFAULT_THRESHOLD
            is_first_check = old_compound is None
            shifted = not is_first_check and (
                old_label != new_label or abs(new_compound - old_compound) >= threshold
            )

            with _db() as conn:
                conn.execute(
                    "UPDATE watches SET last_label = ?, last_compound = ? WHERE id = ?",
                    (new_label, new_compound, w["id"]),
                )

            if not shifted:
                continue

            message = _format_message(symbol, old_label, new_label, old_compound, new_compound)
            event_payload = {
                "symbol": symbol,
                "old_label": old_label,
                "new_label": new_label,
                "old_compound": old_compound,
                "new_compound": new_compound,
                "timestamp": _now_iso(),
            }
            delivered, error = await _deliver(dict(w), message, event_payload)

            with _db() as conn:
                conn.execute(
                    "INSERT INTO alert_events "
                    "(watch_id, symbol, old_label, new_label, old_compound, new_compound, delivered, error, sent_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        w["id"], symbol, old_label, new_label, old_compound,
                        new_compound, int(delivered), error, _now_iso(),
                    ),
                )


async def poll_loop() -> None:
    while True:
        try:
            await _poll_once()
        except Exception:
            pass  # never let a bad round kill the loop -- retry next interval
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
