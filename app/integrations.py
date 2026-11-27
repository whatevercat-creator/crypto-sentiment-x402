"""
Inbound integration for TradingView alert webhooks.

TradingView alerts can POST a JSON (or plain-text) message to a URL you
choose when the alert fires -- but TradingView does not let you attach
custom headers, so the API key can't go in an X-API-Key header. It goes
in the alert message instead. Point a TradingView alert's "Webhook URL"
field at:

    https://<your-deployment>/integrations/tradingview

and set the alert message to JSON with your API key and the ticker, e.g.:

    {"api_key": "csk_...", "symbol": "{{ticker}}", "price": "{{close}}", "time": "{{time}}"}

The key is read from the message and removed from it before anything is
relayed, so it never reaches your webhook, Discord or Telegram.

When the alert fires, this endpoint looks up the *current* sentiment for
that symbol and relays a combined "price alert + sentiment" message to
every active watch this API key has for that symbol (configured the
normal way via POST /alerts/watch) -- same delivery channels (webhook /
Discord / Telegram) as the sentiment-shift alerts in app/alerts.py.

Deprecated: the older form put the key in the URL path,
POST /integrations/tradingview/<your-api-key>. It still works, but a URL
path ends up in request logs, so its responses carry a "deprecated" field,
each call logs one tradingview_path_key line (the key's first 8
characters only, never the full key), and the access log shows the path
as /integrations/tradingview/[redacted] (AccessLogKeyRedactor below). The
path form will be removed once the logs show no tradingview_path_key
line for 60 days.

This only enriches TradingView alerts you already configured a watch for
-- it does not create watches on its own, since a watch also carries the
delivery channel (where to send it) and the shift threshold.

There is no way to pull this API's data *into* a TradingView chart /
Pine Script indicator -- Pine Script cannot make outbound HTTP calls, so
that direction genuinely isn't possible without TradingView hosting your
data feed directly. This endpoint covers the direction that IS possible:
TradingView alert -> enriched with our sentiment -> your webhook/Discord/
Telegram.

For Zapier: no separate integration needed here. Zapier's "Webhooks by
Zapier" trigger can consume the existing /alerts/watch webhook channel
directly -- see ZAPIER.md.
"""

import json
import logging
import re
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request

from app.billing import _db, get_key_info
from app.coins import COIN_NAMES, SYMBOL_ALIASES, validate_symbol
from app.sentiment_service import compute_sentiment_payload
from app.window import INSUFFICIENT_LABEL
from app.alerts import ACTIVE_WATCHES_SQL, _deliver

logger = logging.getLogger("app.integrations")

router = APIRouter(prefix="/integrations", tags=["integrations"])

# Common suffixes TradingView tickers carry that our coin lookup doesn't
# expect, e.g. "BINANCE:BTCUSDT" or "BTCUSD" should both resolve to "BTC".
_QUOTE_SUFFIXES = ("USDT", "USDC", "BUSD", "USD", "PERP")

# The field in the alert message that carries the API key.
KEY_FIELD = "api_key"
# How much of a path-form key the tradingview_path_key log line keeps
# (same length the key-maintenance scripts print).
KEY_PREFIX_LEN = 8
DEPRECATED_NOTE = (
    "The API key in the URL path is deprecated: URL paths end up in request logs. "
    "Use https://<this-deployment>/integrations/tradingview as the Webhook URL and put "
    'the key in the alert message instead, e.g. {"api_key": "csk_...", "symbol": "{{ticker}}"}. '
    "This form will be removed after 60 days without use."
)
_EXAMPLE_MESSAGE = '{"api_key": "csk_...", "symbol": "{{ticker}}"}'


def _normalize_tv_ticker(raw: str) -> str:
    ticker = raw.strip().upper()
    if ":" in ticker:  # "BINANCE:BTCUSDT" -> "BTCUSDT"
        ticker = ticker.split(":", 1)[1]
    if ticker in COIN_NAMES:
        return ticker
    for suffix in _QUOTE_SUFFIXES:
        if ticker.endswith(suffix) and len(ticker) > len(suffix):
            candidate = ticker[: -len(suffix)]
            if candidate in COIN_NAMES or candidate in SYMBOL_ALIASES:  # "POLUSDT" -> "POL" -> MATIC
                return candidate
    return ticker  # fall through -- validate_symbol/compute_sentiment_payload will handle/reject it


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# Matches the key segment of the deprecated path form wherever it appears in
# a logged path: /integrations/tradingview/<key>[/...][?...].
_PATH_KEY = re.compile(r"(/integrations/tradingview/)[^/?#\s]+")
_REDACTED = r"\1[redacted]"


def redact_path(path: str) -> str:
    """The path with any /integrations/tradingview/<key> segment replaced by
    [redacted]. Other paths come back unchanged."""
    return _PATH_KEY.sub(_REDACTED, path)


class AccessLogKeyRedactor(logging.Filter):
    """Filter for the uvicorn.access logger: redacts the key in the
    deprecated /integrations/tradingview/<key> path before the line is
    written. uvicorn logs '%s - "%s %s HTTP/%s" %d' with the path (and query
    string) as the third argument. Never drops a line and never raises."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            args = record.args
            if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
                record.args = args[:2] + (redact_path(args[2]),) + args[3:]
            elif isinstance(record.msg, str):
                record.msg = redact_path(record.msg)
        except Exception:  # noqa: BLE001 -- a log filter must never break logging
            pass
        return True


def _parse_message(raw_body: str) -> tuple:
    """(parsed JSON object or {}, ticker or None) from a TradingView message."""
    try:
        parsed = json.loads(raw_body)
    except json.JSONDecodeError:
        return {}, None  # TradingView also allows a plain-text message
    if not isinstance(parsed, dict):
        return {}, None
    return parsed, parsed.get("symbol") or parsed.get("ticker")


def _symbol_missing() -> HTTPException:
    return HTTPException(
        400,
        "Couldn't find a symbol in the webhook body. Set the TradingView alert "
        f"message to JSON like {_EXAMPLE_MESSAGE}.",
    )


async def _relay(key_id: str, symbol_raw: str, alert: object) -> dict:
    """Look up current sentiment for the ticker and relay it, with the
    TradingView alert (already free of any API key), to this key's active
    watches for that symbol."""
    normalized = _normalize_tv_ticker(symbol_raw)
    try:
        symbol = validate_symbol(normalized)
    except ValueError as e:
        raise HTTPException(400, str(e))

    try:
        sentiment_payload = await compute_sentiment_payload(symbol)
    except ValueError as e:
        raise HTTPException(400, str(e))

    with _db() as conn:
        watches = conn.execute(
            f"SELECT w.* FROM {ACTIVE_WATCHES_SQL} AND w.api_key = ? AND w.symbol = ?",
            (key_id, symbol),
        ).fetchall()

    if not watches:
        raise HTTPException(
            404,
            f"No active alert watch for {symbol} on this API key -- this endpoint enriches "
            f"and relays through watches you've already set up. Create one first with "
            f"POST /alerts/watch (symbol={symbol!r}, plus the channel to deliver to), then "
            f"point this same TradingView alert at this URL again.",
        )

    label = sentiment_payload["overall_sentiment"]["label"]
    compound = sentiment_payload["overall_sentiment"]["average_compound"]
    # With too little recent news the score isn't meaningful, so the
    # message gives the label alone.
    reading = label if label == INSUFFICIENT_LABEL else f"{label} ({compound:.2f})"
    message = f"\U0001F4C8 TradingView alert fired for {symbol} -- current sentiment: {reading}"
    event_payload = {
        "source": "tradingview",
        "symbol": symbol,
        "tradingview_alert": alert,
        "sentiment_label": label,
        "sentiment_compound": compound,
        "timestamp": _now_iso(),
    }

    results = []
    for w in watches:
        delivered, error = await _deliver(dict(w), message, event_payload)
        results.append({"watch_id": w["id"], "channel_type": w["channel_type"], "delivered": delivered, "error": error or None})

    return {
        "symbol": symbol,
        "sentiment_label": label,
        "sentiment_compound": compound,
        "relayed_to": results,
    }


def _without_key(parsed: dict) -> dict:
    """The alert message with the API key field removed: what gets relayed."""
    return {k: v for k, v in parsed.items() if k != KEY_FIELD}


@router.post("/tradingview", include_in_schema=False)
async def tradingview_webhook(request: Request):
    """The API key travels in the JSON alert message ("api_key"), never in
    the URL, so it can't end up in request logs."""
    raw_body = (await request.body()).decode("utf-8", errors="replace").strip()
    parsed, ticker = _parse_message(raw_body)
    api_key = parsed.get(KEY_FIELD)
    if not isinstance(api_key, str) or not api_key.strip():
        raise HTTPException(
            401,
            "Missing API key. Set the TradingView alert message to JSON with your key, "
            f"like {_EXAMPLE_MESSAGE}.",
        )
    key_id = get_key_info(api_key.strip())["key_id"]  # raises 401/403 for invalid/inactive keys
    if not ticker:
        # No plain-text fallback here: the message is JSON (it carries the key).
        raise _symbol_missing()
    return await _relay(key_id, ticker, _without_key(parsed))


@router.post("/tradingview/{api_key}", include_in_schema=False)
async def tradingview_webhook_path_key(api_key: str, request: Request):
    """Deprecated: the key in the URL path. Kept working until the logs show
    no tradingview_path_key line for 60 days (see the module docstring)."""
    logger.info(json.dumps({"event": "tradingview_path_key", "key_prefix": api_key[:KEY_PREFIX_LEN]}))
    key_id = get_key_info(api_key)["key_id"]  # raises 401/403 for invalid/inactive keys

    raw_body = (await request.body()).decode("utf-8", errors="replace").strip()
    if not raw_body:
        raise HTTPException(400, "Empty webhook body. Set the TradingView alert message to JSON "
                                  "like {\"symbol\": \"{{ticker}}\"}.")

    parsed, ticker = _parse_message(raw_body)
    # A JSON message is relayed without any api_key field; a plain-text one
    # (no JSON object) as sent.
    alert: object = _without_key(parsed) if parsed else raw_body

    if not ticker:
        # Plain-text fallback: treat the whole body as the ticker if it's short
        # and looks like one (TradingView plain-text alerts are just the message).
        # Not for a JSON object, which may carry a key.
        if not parsed and len(raw_body) <= 20 and " " not in raw_body:
            ticker = raw_body
        else:
            raise _symbol_missing()

    result = await _relay(key_id, ticker, alert)
    result["deprecated"] = DEPRECATED_NOTE
    return result
