"""
Crypto Sentiment API — x402-gated, with an optional Stripe-subscription lane

Free-source aggregate crypto sentiment (Reddit + crypto news RSS + Fear &
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

from fastapi import FastAPI, HTTPException, Header
from fastapi.responses import JSONResponse

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
    "GET /sentiment/*": RouteConfig(
        accepts=[
            PaymentOption(
                scheme="exact",
                price=PRICE_USD,
                network=CAIP2_NETWORK,
                pay_to=PAY_TO_ADDRESS,
            ),
        ],
        description="Real-time crypto sentiment for a ticker symbol (e.g. BTC, ETH, SOL). Aggregates Reddit, crypto news (CoinDesk, Cointelegraph, Decrypt), and the Fear & Greed Index. Returns a bullish/bearish/neutral label, sentiment score, and per-source breakdown as JSON. Useful for trading bots and market research agents. Path param: symbol, e.g. /sentiment/BTC.",
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

app = FastAPI(title="Crypto Sentiment API (x402)")
app.add_middleware(PaymentMiddlewareASGI, routes=routes, server=server)
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


@app.get("/")
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


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/sentiment/{symbol}")
async def get_sentiment(symbol: str):
    """x402 pay-per-call lane -- gated by PaymentMiddlewareASGI above."""
    try:
        payload = await compute_sentiment_payload(symbol)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return JSONResponse(payload)


@app.get("/v1/sentiment/{symbol}")
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
