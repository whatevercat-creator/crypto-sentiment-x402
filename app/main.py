"""
Crypto Sentiment API — x402-gated, with an optional Stripe-subscription lane

Free-source aggregate crypto sentiment (8 crypto news RSS outlets + Fear &
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
  (Stripe subscription env vars are documented in app/billing.py)
"""

import os
import asyncio
import logging

logging.basicConfig(level=logging.INFO)

from fastapi import FastAPI, HTTPException, Header
from fastapi.responses import JSONResponse, PlainTextResponse
from starlette.middleware.base import BaseHTTPMiddleware

from cdp.x402 import create_facilitator_config

from x402.http import HTTPFacilitatorClient, PaymentOption
from x402.http.middleware.fastapi import PaymentMiddlewareASGI
from x402.http.types import RouteConfig
from x402.server import x402ResourceServer
from x402.mechanisms.evm.exact import ExactEvmServerScheme
from x402.extensions.bazaar import (
    declare_discovery_extension,
    OutputConfig,
    bazaar_resource_server_extension,
)

from app.billing import router as billing_router, init_db, verify_and_charge_api_key
from app.alerts import router as alerts_router, init_alerts_db, poll_loop
from app.dataset import router as dataset_router, init_dataset_db, snapshot_loop
from app.rapidapi import router as rapidapi_router
from app.integrations import router as integrations_router
from app.sentiment_service import compute_sentiment_payload

PAY_TO_ADDRESS = os.environ.get("PAY_TO_ADDRESS")
NETWORK_MODE = os.environ.get("X402_NETWORK", "testnet")
PRICE_USD = os.environ.get("X402_PRICE_USD", "$0.01")
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

CAIP2_NETWORK = "eip155:8453" if NETWORK_MODE == "mainnet" else "eip155:84532"

facilitator = HTTPFacilitatorClient(create_facilitator_config())
server = x402ResourceServer(facilitator)
server.register(CAIP2_NETWORK, ExactEvmServerScheme())
server.register_extension(bazaar_resource_server_extension)

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
        description="Real-time crypto sentiment for a ticker symbol (e.g. BTC, ETH, SOL). Aggregates 8 crypto news RSS outlets (CoinDesk, Cointelegraph, Decrypt, Bitcoin Magazine, The Block, CryptoSlate, NewsBTC, CryptoPotato) and the Fear & Greed Index. Returns a bullish/bearish/neutral label, sentiment score, and per-source breakdown as JSON. Useful for trading bots and market research agents. Path param: symbol, e.g. /sentiment/BTC.",
        mime_type="application/json",
        extensions={
            **declare_discovery_extension(
                input={"method": "GET", "symbol": "BTC"},
                input_schema={
                    "properties": {
                        "method": {
                            "type": "string",
                            "description": "HTTP method, always GET",
                        },
                        "symbol": {
                            "type": "string",
                            "description": "Uppercase ticker symbol, e.g. BTC, ETH, SOL",
                        },
                    },
                    "required": ["method", "symbol"],
                },
                output=OutputConfig(
                    example={
                        "symbol": "BTC",
                        "name": "Bitcoin",
                        "overall_sentiment": {
                            "label": "bullish",
                            "average_compound": 0.21,
                        },
                    },
                    schema={
                        "properties": {
                            "symbol": {"type": "string"},
                            "name": {"type": "string"},
                            "overall_sentiment": {"type": "object"},
                        },
                        "required": ["symbol", "overall_sentiment"],
                    },
                ),
            )
        },
    ),
    # NOTE: /v1/sentiment/* is intentionally NOT listed here -- it's the
    # Stripe-subscription lane, gated by verify_and_charge_api_key() below
    # instead of the x402 payment middleware.
}

class AddWWWAuthenticateMiddleware(BaseHTTPMiddleware):
    """Adds a WWW-Authenticate: Payment header to 402 responses.

    Not part of the x402 v2 header set (PAYMENT-REQUIRED/-SIGNATURE/-RESPONSE
    already carry everything an x402-aware client needs) -- this is purely
    so a generic HTTP client that only understands RFC 9110 can tell a 402
    means "payment needed" without knowing about x402 at all. Registered
    after PaymentMiddlewareASGI so it wraps around it and can see/amend the
    402 response PaymentMiddlewareASGI returns.
    """

    async def dispatch(self, request, call_next):
        response = await call_next(request)
        if response.status_code == 402:
            response.headers["WWW-Authenticate"] = "Payment"
        return response


app = FastAPI(
    title="Crypto Sentiment API (x402)",
    contact={"email": "whatevercat@gmail.com"},
)
app.add_middleware(PaymentMiddlewareASGI, routes=routes, server=server)
app.add_middleware(AddWWWAuthenticateMiddleware)
app.include_router(billing_router)
app.include_router(alerts_router)
app.include_router(dataset_router)
app.include_router(rapidapi_router)
app.include_router(integrations_router)

_alert_task = None
_snapshot_task = None


@app.on_event("startup")
async def _startup():
    global _alert_task, _snapshot_task
    init_db()
    init_alerts_db()
    init_dataset_db()
    _alert_task = asyncio.create_task(poll_loop())
    _snapshot_task = asyncio.create_task(snapshot_loop())


@app.on_event("shutdown")
async def _shutdown():
    for task in (_alert_task, _snapshot_task):
        if task is not None:
            task.cancel()


@app.get("/", openapi_extra={"security": []})
async def root():
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
        "docs": "/docs",
    }


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

## Alternative access (not x402)
- GET /v1/sentiment/{{symbol}} -- X-API-Key header, Stripe subscription quota
  (see /billing/pricing)
- GET /rapidapi/sentiment/{{symbol}} -- RapidAPI-proxied traffic only

## Discovery
- OpenAPI spec: /openapi.json
- x402 discovery manifest: /.well-known/x402
- Interactive docs: /docs
- Transparency: /transparency

## Data sources
8 crypto news RSS outlets (CoinDesk, Cointelegraph, Decrypt, Bitcoin
Magazine, The Block, CryptoSlate, NewsBTC, CryptoPotato) and the Fear &
Greed Index (alternative.me). Scored with VADER sentiment analysis plus a
crypto slang lexicon. Reddit is intentionally not used -- see /transparency.

## Contact
whatevercat@gmail.com
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
            }
        ],
        "attestation": {"type": "none"},
        "docs": f"{PUBLIC_BASE_URL}/docs",
        "contact": "whatevercat@gmail.com",
        "updated": "2026-09-18T00:00:00Z",
    }


@app.get("/.well-known/security.txt", openapi_extra={"security": []})
async def security_txt():
    """RFC 9116 security contact file."""
    body = (
        "Contact: mailto:whatevercat@gmail.com\n"
        "Expires: 2027-09-18T00:00:00.000Z\n"
        "Preferred-Languages: en\n"
    )
    return PlainTextResponse(body)


@app.get("/transparency", openapi_extra={"security": []})
async def transparency():
    return {
        "operator": "Independently run by a solo developer",
        "contact": "whatevercat@gmail.com",
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
        "no_hidden_fees": (
            "The price quoted in the x402 402 response is the full price -- "
            "no additional fees are added at settlement."
        ),
    }


@app.get("/sentiment/{symbol}")
async def get_sentiment(symbol: str):
    """x402 pay-per-call lane -- gated by PaymentMiddlewareASGI above."""
    try:
        payload = await compute_sentiment_payload(symbol)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return JSONResponse(payload)


@app.get("/v1/sentiment/{symbol}", openapi_extra={"security": []})
async def get_sentiment_v1(symbol: str, x_api_key: str = Header(..., alias="X-API-Key")):
    """Stripe-subscription lane -- gated by an API key issued via /billing/*."""
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
