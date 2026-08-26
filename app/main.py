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

from app.sources.reddit import fetch_reddit_posts
from app.sources.news import fetch_news_headlines
from app.sources.feargreed import fetch_fear_greed
from app.sentiment import score_texts
from app.coins import resolve_name

# ---------------------------------------------------------------------
# x402 configuration
#
# We always use the CDP (Coinbase Developer Platform) facilitator, for
# both testnet and mainnet — Coinbase recommends this over the old
# x402.org testnet-only facilitator, which now requires auth it doesn't
# provide by default. CDP is free to sign up for and has a generous
# free tier (1,000 onchain transactions/month).
# ---------------------------------------------------------------------

PAY_TO_ADDRESS = os.environ.get("PAY_TO_ADDRESS")
NETWORK_MODE = os.environ.get("X402_NETWORK", "testnet")  # "testnet" | "mainnet"
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

# create_facilitator_config() reads CDP_API_KEY_ID / CDP_API_KEY_SECRET
# from the environment and builds an authenticated config for the CDP
# Facilitator automatically.
facilitator = HTTPFacilitatorClient(create_facilitator_config())
server = x402ResourceServer(facilitator)
server.register(CAIP2_NETWORK, ExactEvmServerScheme())

routes = {
    "GET /sentiment/{symbol}": RouteConfig(
        accepts=[
            PaymentOption(
                scheme="exact",
                price=PRICE_USD,
                network=CAIP2_NETWORK,
                pay_to=PAY_TO_ADDRESS,
            ),
        ],
        description=(
            "Real-time crypto market sentiment analysis for a given ticker "
            "symbol (e.g. BTC, ETH, SOL, DOGE). Aggregates and scores live "
            "posts and headlines from Reddit crypto communities, major "
            "crypto news outlets (CoinDesk, Cointelegraph, Decrypt), and "
            "the Fear & Greed Index. Returns a bullish/bearish/neutral "
            "label, a numeric sentiment score, positive/negative/neutral "
            "post percentages, and a per-source breakdown as JSON. Useful "
            "for trading bots, market research agents, portfolio "
            "monitoring, and social sentiment tracking. Path parameter: "
            "symbol (uppercase ticker, e.g. /sentiment/BTC)."
        ),
    ),
}

app = FastAPI(title="Crypto Sentiment API (x402)")
app.add_middleware(PaymentMiddlewareASGI, routes=routes, server=server)


# ---------------------------------------------------------------------
# Free, unpaywalled endpoints (health check + human-readable docs)
# ---------------------------------------------------------------------

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


# ---------------------------------------------------------------------
# Paid endpoint — payment is enforced by the middleware above; this
# handler only runs after a valid payment has been verified.
# ---------------------------------------------------------------------

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
