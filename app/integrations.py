"""
Inbound integration for TradingView alert webhooks.

TradingView alerts can POST a JSON (or plain-text) message to a URL you
choose when the alert fires -- but TradingView does not let you attach
custom headers, so the API key has to travel in the URL path instead of
an X-API-Key header (the same workaround every TradingView-webhook guide
uses). Point a TradingView alert's "Webhook URL" field at:

    https://<your-deployment>/integrations/tradingview/<your-api-key>

and set the alert message to JSON that includes the ticker, e.g.:

    {"symbol": "{{ticker}}", "price": "{{close}}", "time": "{{time}}"}

When the alert fires, this endpoint looks up the *current* sentiment for
that symbol and relays a combined "price alert + sentiment" message to
every active watch this API key has for that symbol (configured the
normal way via POST /alerts/watch) -- same delivery channels (webhook /
Discord / Telegram) as the sentiment-shift alerts in app/alerts.py.

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
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request

from app.billing import _db, get_key_info
from app.coins import COIN_NAMES, validate_symbol
from app.sentiment_service import compute_sentiment_payload
from app.alerts import _deliver

router = APIRouter(prefix="/integrations", tags=["integrations"])

# Common suffixes TradingView tickers carry that our coin lookup doesn't
# expect, e.g. "BINANCE:BTCUSDT" or "BTCUSD" should both resolve to "BTC".
_QUOTE_SUFFIXES = ("USDT", "USDC", "BUSD", "USD", "PERP")


def _normalize_tv_ticker(raw: str) -> str:
    ticker = raw.strip().upper()
    if ":" in ticker:  # "BINANCE:BTCUSDT" -> "BTCUSDT"
        ticker = ticker.split(":", 1)[1]
    if ticker in COIN_NAMES:
        return ticker
    for suffix in _QUOTE_SUFFIXES:
        if ticker.endswith(suffix) and len(ticker) > len(suffix):
            candidate = ticker[: -len(suffix)]
            if candidate in COIN_NAMES:
                return candidate
    return ticker  # fall through -- validate_symbol/compute_sentiment_payload will handle/reject it


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@router.post("/tradingview/{api_key}")
async def tradingview_webhook(api_key: str, request: Request):
    key_info = get_key_info(api_key)  # raises 401/403 for invalid/inactive keys

    raw_body = (await request.body()).decode("utf-8", errors="replace").strip()
    if not raw_body:
        raise HTTPException(400, "Empty webhook body. Set the TradingView alert message to JSON "
                                  "like {\"symbol\": \"{{ticker}}\"}.")

    ticker = None
    parsed: dict = {}
    try:
        parsed = json.loads(raw_body)
        if isinstance(parsed, dict):
            ticker = parsed.get("symbol") or parsed.get("ticker")
    except json.JSONDecodeError:
        pass  # TradingView also allows a plain-text message -- fall back below

    if not ticker:
        # Plain-text fallback: treat the whole body as the ticker if it's short
        # and looks like one (TradingView plain-text alerts are just the message).
        if len(raw_body) <= 20 and " " not in raw_body:
            ticker = raw_body
        else:
            raise HTTPException(
                400,
                "Couldn't find a symbol in the webhook body. Set the TradingView alert "
                "message to JSON like {\"symbol\": \"{{ticker}}\"}.",
            )

    normalized = _normalize_tv_ticker(ticker)
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
            "SELECT * FROM watches WHERE api_key = ? AND symbol = ? AND status = 'active'",
            (api_key, symbol),
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
    message = (
        f"\U0001F4C8 TradingView alert fired for {symbol} -- current sentiment: "
        f"{label} ({compound:.2f})"
    )
    event_payload = {
        "source": "tradingview",
        "symbol": symbol,
        "tradingview_alert": parsed or raw_body,
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
