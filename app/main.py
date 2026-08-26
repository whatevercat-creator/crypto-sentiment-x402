"""
Crypto Sentiment API — x402-gated

Free-source aggregate crypto sentiment (Reddit + crypto news RSS + Fear &
Greed Index), scored with VADER + a crypto slang lexicon, sold per-call
to AI agents via the x402 payment protocol (Base + USDC).

Env vars (see .env.example):
  PAY_TO_ADDRESS       - your Base wallet address that receives USDC (required)
  X402_NETWORK         - "testnet" (default) or "mainnet"
  X402_PRICE_USD       - price per call, e.g. "$0.01" (default)
  CDP_API_KEY_ID        - required (CDP facilitator handles both testnet & mainnet)
  CDP_API_KEY_SECRET    - required
"""

import os
import asyncio

from fastapi import FastAPI, HTTPException
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

from app.sources.reddit import fetch_reddit_posts
from app.sources.news import fetch_news_headlines
from app.sources.feargreed import fetch_fear_greed
from app.sentiment import score_texts
from app.coins import resolve_name

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
                input={"symbol": "BTC"},
                input_schema={
                    "properties": {
                        "symbol": {
                            "type": "string",
                            "description": "Uppercase ticker symbol, e.g. BTC, ETH, SOL",
                        }
                    },
                    "required": ["symbol"],
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
}

app = FastAPI(title="Crypto Sentiment API (x402)")
app.add_middleware(PaymentMiddlewareASGI, routes=routes, server=server)


@app.get("/")
async def root():
    return {
        "name": "Crypto Sentiment API",
        "protocol": "x402",
        "network": NETWORK_MODE,
        "price_per_call": PRICE_USD,
        "paid_endpoint": "/sentiment/{symbol}",
        "example": "/sentiment/BTC",
        "docs": "/docs",
    }


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/sentiment/{symbol}")
async def get_sentiment(symbol: str):
    symbol = symbol.upper().strip()
    if not symbol.isalnum() or len(symbol) > 10:
        raise HTTPException(status_code=400, detail="Invalid symbol")

    name = resolve_name(symbol)

    reddit_task = fetch_reddit_posts(symbol)
    news_task = fetch_news_headlines(symbol, name)
    fng_task = fetch_fear_greed()

    reddit_texts, news_texts, fear_greed = await asyncio.gather(
        reddit_task, news_task, fng_task
    )

    all_texts = reddit_texts + news_texts
    overall = score_texts(all_texts)
    reddit_score = score_texts(reddit_texts)
    news_score = score_texts(news_texts)

    return JSONResponse(
        {
            "symbol": symbol,
            "name": name,
            "overall_sentiment": overall,
            "breakdown": {
                "reddit": reddit_score,
                "news": news_score,
                "fear_greed_index": fear_greed,
            },
            "sources": [
                "reddit.com (r/CryptoCurrency, r/Bitcoin, r/CryptoMarkets)",
                "coindesk.com RSS",
                "cointelegraph.com RSS",
                "decrypt.co RSS",
                "alternative.me Fear & Greed Index",
            ],
        }
    )
