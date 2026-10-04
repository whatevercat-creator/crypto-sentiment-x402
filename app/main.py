"""
Crypto Sentiment API — x402-gated, with an optional Stripe-subscription lane

Free-source aggregate crypto sentiment (10 crypto news RSS outlets + Fear &
Greed Index), scored with VADER + a crypto slang lexicon.

Two ways to buy it:
  - GET /sentiment/{symbol}     -- x402 pay-per-call in USDC on Base (agents)
  - GET /v1/sentiment/{symbol}  -- X-API-Key header, Stripe subscription
                                    quota (humans/devs who don't want crypto)
                                    -- see app/billing.py and BILLING.md

Env vars (see .env.example):
  PAY_TO_ADDRESS       - your Base wallet address that receives USDC (required)
  X402_NETWORK         - "testnet" (default) or "mainnet"
  X402_PRICE_USD       - price per call, e.g. "$0.01" (default)
  CDP_API_KEY_ID        - required (CDP facilitator handles both testnet & mainnet)
  CDP_API_KEY_SECRET    - required
  (Hot->cold wallet sweep env vars are documented in app/sweep.py)
  (Stripe subscription env vars are documented in app/billing.py)
"""

import os
import json
import base64
import binascii
import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import unquote

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("app.main")

from fastapi import FastAPI, HTTPException, Header, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware

from cdp.x402 import create_facilitator_config

from x402.http import HTTPFacilitatorClient, PaymentOption
from x402.http.middleware.fastapi import PaymentMiddlewareASGI
from x402.http.types import PaywallConfig, RouteConfig
from x402.server import x402ResourceServer
from x402.mechanisms.evm.exact import ExactEvmServerScheme
from x402.extensions.bazaar import (
    declare_discovery_extension,
    OutputConfig,
    bazaar_resource_server_extension,
)

from app.billing import (
    router as billing_router,
    init_db,
    verify_and_charge_api_key,
    pricing as billing_pricing,
    TIERS,
    X402_PRICE_USD,
    API_KEY_SCHEME,
    API_KEY_SECURITY,
)
from app.alerts import router as alerts_router, init_alerts_db, poll_loop
from app.dataset import router as dataset_router, init_dataset_db, snapshot_loop
from app import archive
from app.home import render_home
from app.hourly import init_hourly_db, hourly_loop
from app.rapidapi import router as rapidapi_router, RAPIDAPI_SCHEME
from app.integrations import router as integrations_router
from app.coins import validate_symbol
from app.sentiment_service import compute_sentiment_payload
from app.sources.news import NEWS_OUTLETS
from app.sweep import sweep_loop

PAY_TO_ADDRESS = os.environ.get("PAY_TO_ADDRESS")
NETWORK_MODE = os.environ.get("X402_NETWORK", "testnet")
PRICE_USD = X402_PRICE_USD
PUBLIC_BASE_URL = os.environ.get(
    "APP_BASE_URL", "https://crypto-sentiment-x402.onrender.com"
).rstrip("/")

if not PAY_TO_ADDRESS:
    raise RuntimeError(
        "PAY_TO_ADDRESS env var is required — set it to the Base wallet "
        "address that should receive USDC payments."
    )

if not os.environ.get("CDP_API_KEY_ID") or not os.environ.get("CDP_API_KEY_SECRET"):
    raise RuntimeError(
        "CDP_API_KEY_ID and CDP_API_KEY_SECRET env vars are required. "
        "Get a free API key at https://portal.cdp.coinbase.com"
    )

# Example /sentiment response, shared by the Bazaar discovery metadata, the
# OpenAPI spec and the HTML home page. Sample headlines and example.com
# links, not real articles.
EXAMPLE_RESPONSE = {
    "symbol": "BTC",
    "name": "Bitcoin",
    "overall_sentiment": {
        "label": "bullish",
        "average_compound": 0.21,
    },
    "drivers": [
        {
            "title": "Bitcoin ETFs log fifth straight day of inflows",
            "source": "CoinDesk",
            "link": "https://example.com/news/bitcoin-etf-inflows",
            "published": "2026-10-03T14:05:00Z",
            "score": 0.6249,
        },
        {
            "title": "Exchange hack drains $40M as bitcoin dips",
            "source": "The Block",
            "link": "https://example.com/news/exchange-hack",
            "published": "2026-10-03T12:40:00Z",
            "score": -0.5719,
        },
        {
            "title": "Bitcoin miners report record hashrate",
            "source": "Decrypt",
            "link": "https://example.com/news/record-hashrate",
            "published": "2026-10-03T11:15:00Z",
            "score": 0.4404,
        },
    ],
    "drivers_summary": "2 of the top 3 headlines are positive, 1 is negative.",
}

_NULLABLE_STRING = {"type": ["string", "null"]}
DRIVERS_SCHEMA = {
    "type": "array",
    "maxItems": 5,
    "items": {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "source": {"type": "string"},
            "link": _NULLABLE_STRING,
            "published": _NULLABLE_STRING,
            "score": {"type": "number"},
        },
        "required": ["title", "source", "link", "published", "score"],
    },
}

CAIP2_NETWORK = "eip155:8453" if NETWORK_MODE == "mainnet" else "eip155:84532"

facilitator = HTTPFacilitatorClient(create_facilitator_config())
server = x402ResourceServer(facilitator)
server.register(CAIP2_NETWORK, ExactEvmServerScheme())
server.register_extension(bazaar_resource_server_extension)

SENTIMENT_DISCOVERY = declare_discovery_extension(
    # symbol is a PATH param (/sentiment/BTC), not a query param.
    # No query params at all.
    path_params_schema={
        "properties": {
            "symbol": {
                "type": "string",
                "description": "Uppercase ticker symbol in the URL path, e.g. BTC in /sentiment/BTC",
            },
        },
        "required": ["symbol"],
    },
    output=OutputConfig(
        example=EXAMPLE_RESPONSE,
        schema={
            "properties": {
                "symbol": {"type": "string"},
                "name": {"type": "string"},
                "overall_sentiment": {"type": "object"},
                "drivers": DRIVERS_SCHEMA,
                "drivers_summary": {"type": "string"},
            },
            "required": ["symbol", "overall_sentiment"],
        },
    ),
)
# declare_discovery_extension() leaves out info.input.method (the library
# fills it in per request), so the x402 middleware's startup check rejects
# the extension as declared with "input: 'method' is a required property".
# Declare the method and an example path param up front so the extension is
# valid on its own. Per request the library still overwrites both with the
# real method and symbol, so the 402 response is unchanged.
SENTIMENT_DISCOVERY["bazaar"]["info"]["input"].update(
    method="GET",
    pathParams={"symbol": EXAMPLE_RESPONSE["symbol"]},
)

HISTORY_DISCOVERY = declare_discovery_extension(
    # No example query params: CDP's probe then sends none, and an unpaid
    # request gets the 402 whatever the range.
    input_schema={
        "properties": {
            "start": {"type": "string", "description": "ISO 8601 start time (UTC if no offset). Default: end minus 7 days."},
            "end": {"type": "string", "description": "ISO 8601 end time, exclusive. Default: now. At most 30 days after start."},
        },
    },
    path_params_schema={
        "properties": {
            "symbol": {
                "type": "string",
                "description": "Uppercase ticker symbol in the URL path, e.g. BTC in /history/BTC. See /archive for which symbols are logged.",
            },
        },
        "required": ["symbol"],
    },
    output=OutputConfig(
        example=archive.HISTORY_EXAMPLE,
        schema={
            "properties": {
                "symbol": {"type": "string"},
                "start": {"type": "string"},
                "end": {"type": "string"},
                "count": {"type": "integer"},
                "rows": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "observed_at": {"type": "string"},
                            "average_compound": {"type": ["number", "null"]},
                            "sample_size": {"type": ["integer", "null"]},
                            "fear_greed_value": {"type": ["integer", "null"]},
                            "matcher": _NULLABLE_STRING,
                            "drivers": {"anyOf": [{"type": "null"}, DRIVERS_SCHEMA]},
                        },
                        "required": ["observed_at", "average_compound", "matcher", "drivers"],
                    },
                },
            },
            "required": ["symbol", "start", "end", "count", "rows"],
        },
    ),
)
# Same reason as SENTIMENT_DISCOVERY above.
HISTORY_DISCOVERY["bazaar"]["info"]["input"].update(
    method="GET",
    pathParams={"symbol": archive.HISTORY_EXAMPLE["symbol"]},
)

routes = {
    "GET /sentiment/:symbol": RouteConfig(
        accepts=[
            PaymentOption(
                scheme="exact",
                price=PRICE_USD,
                network=CAIP2_NETWORK,
                pay_to=PAY_TO_ADDRESS,
            ),
        ],
        description="Real-time crypto sentiment for a ticker symbol (e.g. BTC, ETH, SOL). Aggregates 10 crypto news RSS outlets (CoinDesk, Cointelegraph, Decrypt, Bitcoin Magazine, The Block, CryptoSlate, NewsBTC, CryptoPotato, The Defiant, DL News) and the Fear & Greed Index. Returns a bullish/bearish/neutral label, score, per-source breakdown and the top 5 headlines behind it (title, link) as JSON. For trading bots and research agents. Path param: symbol, e.g. /sentiment/BTC.",
        mime_type="application/json",
        extensions=SENTIMENT_DISCOVERY,
    ),
    "GET /history/:symbol": RouteConfig(
        accepts=[
            PaymentOption(
                scheme="exact",
                price=archive.HISTORY_PRICE_USD,
                network=CAIP2_NETWORK,
                pay_to=PAY_TO_ADDRESS,
            ),
        ],
        description="Hourly crypto news-sentiment readings for a ticker (e.g. BTC, ETH), recorded live at the top of every UTC hour and never backfilled. Each row: observed_at, average_compound (-1 to 1), sample_size, Fear & Greed value, matcher and the top 3 headlines behind it (title, link). Query start/end as ISO 8601; default last 7 days, max 30 days per call. See /archive for symbols and coverage.",
        mime_type="application/json",
        extensions=HISTORY_DISCOVERY,
    ),
    # NOTE: /v1/sentiment/* is intentionally NOT listed here -- it's the
    # Stripe-subscription lane, gated by verify_and_charge_api_key() below
    # instead of the x402 payment middleware.
}


class Mirror402ChallengeMiddleware(BaseHTTPMiddleware):
    """Mirrors the PAYMENT-REQUIRED challenge into the 402 body.

    Registered after PaymentMiddlewareASGI so it wraps around it and can
    see/amend the 402 response PaymentMiddlewareASGI returns.

    No WWW-Authenticate header: "Payment" there is the MPP auth scheme, which
    needs a server-bound challenge and Authorization: Payment credentials we
    don't accept, and a bare one fails discovery audits (x402scan). x402
    itself doesn't use WWW-Authenticate, and HTTP only requires it on 401.
    """

    async def dispatch(self, request, call_next):
        response = await call_next(request)
        if response.status_code != 402:
            return response

        # PaymentMiddlewareASGI puts the challenge only in the base64
        # PAYMENT-REQUIRED header and returns an empty {} body. Some x402
        # clients read accepts[] from the body, so mirror the decoded header
        # there. Header is untouched and stays the source of truth.
        body = b"".join([chunk async for chunk in response.body_iterator])
        header = response.headers.get("payment-required")
        if header and body.strip() in (b"", b"{}"):
            try:
                body = json.dumps(json.loads(base64.b64decode(header))).encode()
            except (ValueError, binascii.Error):
                logger.warning("could not decode PAYMENT-REQUIRED header; leaving 402 body as-is")
        headers = {k: v for k, v in response.headers.items() if k.lower() != "content-length"}
        return Response(
            content=body,
            status_code=402,
            headers=headers,
            media_type="application/json",
        )


app = FastAPI(
    title="Crypto Sentiment API (x402)",
    contact={"email": "hi@forgealone.com"},
)
def _decode_b64_json(header: Optional[str]) -> Optional[dict]:
    if not header:
        return None
    try:
        data = json.loads(base64.b64decode(header))
    except (ValueError, binascii.Error):
        return None
    return data if isinstance(data, dict) else None


def _payer_from_signature(header: Optional[str]) -> Optional[str]:
    """Best-effort payer wallet from the x402 v2 PAYMENT-SIGNATURE header
    (payload.authorization.from for the exact EVM scheme). Only the address
    is read; the signature itself is never logged."""
    data = _decode_b64_json(header) or {}
    payer = ((data.get("payload") or {}).get("authorization") or {}).get("from")
    return payer if isinstance(payer, str) else None


def _settlement_tx(header: Optional[str]) -> Optional[str]:
    """Transaction hash from the PAYMENT-RESPONSE header PaymentMiddlewareASGI
    sets after a successful settlement, if it's there."""
    tx = (_decode_b64_json(header) or {}).get("transaction")
    return tx if isinstance(tx, str) and tx else None


# Paid x402 path prefix -> (log event, price).
_PAID_PREFIXES = {
    "/sentiment/": ("sentiment_paid_call", PRICE_USD),
    "/history/": ("history_paid_call", archive.HISTORY_PRICE_USD),
}


class PaidCallLogMiddleware(BaseHTTPMiddleware):
    """One JSON log line per successful paid /sentiment or /history call.
    Wraps PaymentMiddlewareASGI because settlement happens there after the
    route handler returns: a 2xx from a paid route means the payment
    verified and settled (a failed settlement comes back as a 402). grep the
    logs for "sentiment_paid_call" / "history_paid_call" to count paid usage."""

    async def dispatch(self, request, call_next):
        response = await call_next(request)
        path = request.url.path
        prefix = next((p for p in _PAID_PREFIXES if path.startswith(p)), None)
        if prefix and 200 <= response.status_code < 300:
            event, price = _PAID_PREFIXES[prefix]
            logger.info(json.dumps({
                "event": event,
                "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "symbol": unquote(path[len(prefix):]).upper(),
                "price": price,
                "payer": _payer_from_signature(request.headers.get("payment-signature")),
                "transaction": _settlement_tx(response.headers.get("payment-response")),
            }))
        return response


X_GUIDANCE = (
     "Use this API to get a current news-based sentiment reading for one crypto "
     "ticker (e.g. BTC, ETH, SOL). Call GET /sentiment/{symbol} with the ticker "
     "as the path parameter and no query parameters. It costs $0.01 per call, "
     "paid in USDC on Base via the x402 protocol: an unpaid request returns HTTP "
     "402 with the payment requirements in accepts[], and the same request "
     "retried with a valid payment returns the result. The response is JSON with "
     "a bullish/bearish/neutral label, a numeric sentiment score, and a "
     "per-source breakdown built from 10 crypto news outlets plus the Fear & "
     "Greed Index. Use it as one input for market research or trading agents. "
     "It measures news sentiment only; it is not a price prediction or "
     "financial advice. Past hourly readings: GET /history/{symbol} with optional "
     f"ISO 8601 start/end (default last 7 days, max 30), {archive.HISTORY_PRICE_USD} "
     "per call via x402; GET /archive (free) lists symbols and coverage."
)

_base_openapi = app.openapi


def _openapi_with_guidance():
     schema = _base_openapi()
     schema.setdefault("info", {})["x-guidance"] = X_GUIDANCE
     schema.setdefault("components", {})["securitySchemes"] = {
          API_KEY_SCHEME: {
               "type": "apiKey",
               "in": "header",
               "name": "X-API-Key",
               "description": "Subscription key from POST /billing/signup-free "
               "or POST /billing/checkout/{tier}.",
          },
          RAPIDAPI_SCHEME: {
               "type": "apiKey",
               "in": "header",
               "name": "X-RapidAPI-Proxy-Secret",
               "description": "Sent by RapidAPI's proxy; subscribe on RapidAPI to use this route.",
          },
     }
     return schema


app.openapi = _openapi_with_guidance
app.add_middleware(
       PaymentMiddlewareASGI,
       routes=routes,
       server=server,
       paywall_config=PaywallConfig(
           app_name="Crypto Sentiment API",
           testnet=(NETWORK_MODE != "mainnet"),
       ),
   )
app.add_middleware(Mirror402ChallengeMiddleware)
app.add_middleware(PaidCallLogMiddleware)
app.include_router(billing_router)
app.include_router(alerts_router)
app.include_router(dataset_router)
app.include_router(rapidapi_router)
app.include_router(integrations_router)

_alert_task = None
_snapshot_task = None
_hourly_task = None
_sweep_task = None


@app.on_event("startup")
async def _startup():
    global _alert_task, _snapshot_task, _hourly_task, _sweep_task
    init_db()
    init_alerts_db()
    init_dataset_db()
    init_hourly_db()
    _alert_task = asyncio.create_task(poll_loop())
    _snapshot_task = asyncio.create_task(snapshot_loop())
    _hourly_task = asyncio.create_task(hourly_loop())
    _sweep_task = asyncio.create_task(sweep_loop())


@app.on_event("shutdown")
async def _shutdown():
    for task in (_alert_task, _snapshot_task, _hourly_task, _sweep_task):
        if task is not None:
            task.cancel()


# JSON index for agents/directories; browsers (Accept: text/html) get the
# human-readable page from app/home.py instead. No docstring on purpose: it
# would show up in /openapi.json, which should keep describing only the JSON.
# HEAD too, for uptime monitors; kept out of the schema so /openapi.json
# is unchanged.
@app.head("/", include_in_schema=False)
@app.get("/", openapi_extra={"security": []})
async def root(request: Request):
    if "text/html" in request.headers.get("accept", "").lower():
        html = render_home(
            price_usd=PRICE_USD,
            network_mode=NETWORK_MODE,
            base_url=PUBLIC_BASE_URL,
            outlets=list(NEWS_OUTLETS),
            pricing=billing_pricing(),
            alert_limits={cfg["label"]: cfg["alert_limit"] for cfg in TIERS.values()},
            dataset_plans=[cfg["label"] for cfg in TIERS.values() if cfg["dataset_access"]],
            example_response=EXAMPLE_RESPONSE,
            validation=_load_validation(),
            history_price_usd=archive.HISTORY_PRICE_USD,
        )
        return HTMLResponse(html, headers={"Vary": "Accept"})
    return JSONResponse(_root_index(), headers={"Vary": "Accept"})


def _root_index() -> dict:
    return {
        "name": "Crypto Sentiment API",
        "protocol": "x402",
        "network": NETWORK_MODE,
        "price_per_call": PRICE_USD,
        "paid_endpoint": "/sentiment/{symbol}",
        "example": "/sentiment/BTC",
        "subscriptions": "/billing/pricing",
        "alerts": "/alerts/watch (requires an active Starter/Pro X-API-Key)",
        "dataset": "/dataset/info",
        "rapidapi": "/rapidapi/sentiment/{symbol} (RapidAPI-proxied traffic only)",
        "integrations": "/integrations/tradingview/{api_key} (relays TradingView alerts through your existing /alerts/watch channels)",
        "signal_validation": "/validation (does the score lead or lag price? published whatever it shows)",
        "docs": "/docs",
    }


_VALIDATION_PATH = os.path.join(os.path.dirname(__file__), "validation.json")


def _load_validation() -> dict:
    try:
        with open(_VALIDATION_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"status": "measuring", "detail": "validation results not available yet"}


@app.get("/validation", openapi_extra={"security": []})
async def validation():
    """Lead/lag of the sentiment score vs price. Free. Updated by
    scripts/leadlag.py --out app/validation.json, then committed."""
    return _load_validation()


_FAVICON = (Path(__file__).parent / "static" / "favicon.ico").read_bytes()


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return Response(_FAVICON, media_type="image/x-icon", headers={"Cache-Control": "public, max-age=86400"})


@app.head("/health", include_in_schema=False)
@app.get("/health", openapi_extra={"security": []})
async def health():
    return {"status": "ok"}


@app.get("/llms.txt", openapi_extra={"security": []})
async def llms_txt():
    body = f"""# Crypto Sentiment API

Real-time crypto market sentiment for AI agents, priced and paid per call
in USDC on Base via the x402 protocol. No account, API key, or
subscription required for this lane.

## Paid endpoint (x402)
GET {PUBLIC_BASE_URL}/sentiment/{{symbol}}
Price: {PRICE_USD} USDC per call (see the live HTTP 402 response for the
exact current price -- this text file is not the source of truth)
Network: Base ({NETWORK_MODE}), x402 scheme "exact"
Example: GET /sentiment/BTC

## Hourly archive (x402)
GET {PUBLIC_BASE_URL}/history/{{symbol}}?start=<ISO 8601>&end=<ISO 8601>
Price: {archive.HISTORY_PRICE_USD} USDC per call. Hourly readings recorded live
and never backfilled; default range the last 7 days, max 30 days per call.
Each row has observed_at, average_compound, sample_size, fear_greed_value,
matcher and drivers (top 3 headlines; null for rows before 2026-10-03).
What exists (symbols, first reading, row counts, missing hours): GET /archive
(free).

## Alternative access (not x402)
- GET /v1/sentiment/{{symbol}} -- X-API-Key header, Stripe subscription quota
  (see /billing/pricing)
- GET /rapidapi/sentiment/{{symbol}} -- RapidAPI-proxied traffic only

## Discovery
- OpenAPI spec: /openapi.json
- x402 discovery manifest: /.well-known/x402
- Interactive docs: /docs
- Transparency: /transparency
- Signal validation (does the score lead or lag price?): /validation

## Response
JSON with `overall_sentiment` (label: bullish/bearish/neutral, and
average_compound from -1 to 1), a per-source `breakdown`, and `drivers`: up
to 5 headlines that moved the score most, largest first, each with title,
source, link, published (ISO 8601 UTC) and its own score. Titles and links
only, never article text. `drivers_summary` is one plain line such as
"3 of the top 5 headlines are negative, 2 are positive."

## Data sources
10 crypto news RSS outlets (CoinDesk, Cointelegraph, Decrypt, Bitcoin
Magazine, The Block, CryptoSlate, NewsBTC, CryptoPotato, The Defiant, DL
News) and the Fear & Greed Index (alternative.me). Scored with VADER
sentiment analysis plus a crypto slang lexicon. Reddit is intentionally not
used -- see /transparency.

## Contact
hi@forgealone.com
"""
    return PlainTextResponse(body)


@app.get("/.well-known/x402", openapi_extra={"security": []})
async def well_known_x402():
    """x402 discovery manifest -- see draft-hawkins-x402-dns-discovery."""
    return {
        "x402Version": 2,
        "kind": "resource-server",
        "name": "Crypto Sentiment API",
        "description": "Real-time crypto sentiment for AI agents, paid per call in USDC on Base.",
        "resources": [
            {
                "url": f"{PUBLIC_BASE_URL}/sentiment/{{symbol}}",
                "method": "GET",
                "description": "Real-time crypto sentiment for a ticker symbol, e.g. BTC, ETH, SOL.",
            },
            {
                "url": f"{PUBLIC_BASE_URL}/history/{{symbol}}",
                "method": "GET",
                "description": "Hourly sentiment readings for a ticker over a time range (default 7 days, max 30).",
            },
        ],
        "attestation": {"type": "none"},
        "docs": f"{PUBLIC_BASE_URL}/docs",
        "contact": "hi@forgealone.com",
        "updated": "2026-09-18T00:00:00Z",
    }


@app.get("/.well-known/security.txt", openapi_extra={"security": []})
async def security_txt():
    """RFC 9116 security contact file."""
    body = (
        "Contact: mailto:hi@forgealone.com\n"
        "Expires: 2027-09-18T00:00:00.000Z\n"
        "Preferred-Languages: en\n"
    )
    return PlainTextResponse(body)


@app.get("/transparency", openapi_extra={"security": []})
async def transparency():
    return {
        "operator": "Independently run by a solo developer",
        "contact": "hi@forgealone.com",
        "what_this_api_does": (
            "Aggregates real-time crypto sentiment from public crypto news RSS "
            "feeds and the Fear & Greed Index, scored with VADER sentiment "
            "analysis plus a crypto slang lexicon."
        ),
        "data_sources": [
            "coindesk.com RSS",
            "cointelegraph.com RSS",
            "decrypt.co RSS",
            "bitcoinmagazine.com RSS",
            "theblock.co RSS",
            "cryptoslate.com RSS",
            "newsbtc.com RSS",
            "cryptopotato.com RSS",
            "thedefiant.io RSS",
            "dlnews.com RSS",
            "alternative.me Fear & Greed Index",
        ],
        "sources_intentionally_not_used": {
            "reddit": (
                "Removed -- Reddit's Responsible Builder Policy prohibits "
                "commercial use of their data without written approval, which "
                "this paid API would violate."
            ),
        },
        "pricing": {
            "x402_pay_per_call": {
                "price_usd": PRICE_USD,
                "network": NETWORK_MODE,
                "endpoint": "GET /sentiment/{symbol}",
            },
            "subscriptions": "see /billing/pricing",
        },
        "signal_validation": (
            "Whether the score leads or lags price is measured on hourly "
            "data and published at /validation, including unflattering results."
        ),
        "no_hidden_fees": (
            "The price quoted in the x402 402 response is the full price -- "
            "no additional fees are added at settlement."
        ),
        # Same entries as /validation, from app/validation.json.
        "methodology_changes": _load_validation().get("methodology_changes", []),
    }


SENTIMENT_200 = {
    "description": "Sentiment for the symbol. `drivers` lists up to 5 headlines that "
    "moved the score most (title, source, link, published time and each one's own "
    "score), largest first; titles and links only, never article text. "
    "`drivers_summary` is one plain line about them.",
    "content": {"application/json": {"example": EXAMPLE_RESPONSE}},
}


# Tells x402 directories (x402scan / @agentcash/discovery) this is the paid
# route and what it costs; the 402 challenge itself comes from the paywall.
SENTIMENT_PAYMENT_INFO = {
    "x-payment-info": {
        "price": {"mode": "fixed", "currency": "USD", "amount": PRICE_USD.lstrip("$")},
        "protocols": [{"x402": {}}],
    }
}


@app.get(
    "/sentiment/{symbol}",
    openapi_extra=SENTIMENT_PAYMENT_INFO,
    responses={
        200: SENTIMENT_200,
        402: {
            "description": "Payment required. The x402 payment requirements are in "
            "the PAYMENT-REQUIRED header and mirrored in the JSON body."
        }
    },
)
async def get_sentiment(symbol: str):
    """x402 pay-per-call lane -- gated by PaymentMiddlewareASGI above.

    Returns the label and score plus `drivers`: up to 5 headlines that moved
    the score most (title, source, link, published, score; never article
    text), and `drivers_summary`, one plain line about them."""
    try:
        payload = await compute_sentiment_payload(symbol)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return JSONResponse(payload)


@app.get("/archive", openapi_extra={"security": []})
async def archive_page(request: Request):
    """What the hourly archive holds: symbols, first reading, row counts and
    missing hours. Readings are recorded live and never backfilled. Free.
    Browsers get an HTML page; everything else gets JSON."""
    summary = archive.archive_summary()
    if "text/html" in request.headers.get("accept", "").lower():
        return HTMLResponse(archive.render_archive_html(summary), headers={"Vary": "Accept"})
    return JSONResponse(summary, headers={"Vary": "Accept"})


HISTORY_PAYMENT_INFO = {
    "x-payment-info": {
        "price": {"mode": "fixed", "currency": "USD", "amount": archive.HISTORY_PRICE_USD.lstrip("$")},
        "protocols": [{"x402": {}}],
    }
}


@app.get(
    "/history/{symbol}",
    openapi_extra=HISTORY_PAYMENT_INFO,
    responses={
        200: {
            "description": "Hourly readings in the range, oldest first. `drivers` is "
            "each row's stored top 3 headlines (title, source, link, published, "
            "score), or null for rows recorded before drivers were stored.",
            "content": {"application/json": {"example": archive.HISTORY_EXAMPLE}},
        },
        400: {"description": "Invalid symbol or range. Not charged."},
        402: {
            "description": "Payment required. The x402 payment requirements are in "
            "the PAYMENT-REQUIRED header and mirrored in the JSON body."
        },
        404: {"description": "Symbol not in the archive (see /archive). Not charged."},
    },
)
async def get_history(
    symbol: str,
    start: Optional[str] = Query(None, description="ISO 8601 start (UTC if no offset). Default: end minus 7 days."),
    end: Optional[str] = Query(None, description="ISO 8601 end, exclusive. Default: now. Max 30 days after start."),
):
    """x402 pay-per-call: hourly readings for one symbol. Errors are 4xx, and
    the x402 middleware never settles a payment on a 4xx."""
    try:
        symbol = validate_symbol(symbol)
        start_dt, end_dt = archive.resolve_range(start, end)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not archive.symbol_in_archive(symbol):
        raise HTTPException(
            status_code=404,
            detail=f"No hourly readings for {symbol}. See /archive for the symbols that are logged.",
        )
    return JSONResponse(archive.history_payload(symbol, start_dt, end_dt))


@app.get("/v1/sentiment/{symbol}", openapi_extra=API_KEY_SECURITY, responses={200: SENTIMENT_200})
async def get_sentiment_v1(symbol: str, x_api_key: str = Header(..., alias="X-API-Key")):
    """Stripe-subscription lane -- gated by an API key issued via /billing/*.

    Same response as /sentiment/{symbol}, including `drivers` and
    `drivers_summary`, plus `_billing` usage."""
    usage = verify_and_charge_api_key(x_api_key)
    try:
        payload = await compute_sentiment_payload(symbol)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    payload["_billing"] = {
        "tier": usage["tier"],
        "calls_used_this_period": usage["calls_used"],
        "calls_limit_this_period": usage["limit"],
    }
    return JSONResponse(payload)
