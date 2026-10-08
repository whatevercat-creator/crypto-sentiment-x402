"""
RapidAPI-facing endpoint.

RapidAPI works by proxying all traffic to your real API and handling
billing/metering entirely on their side -- buyers subscribe to a plan on
RapidAPI, RapidAPI calls this endpoint on their behalf, and RapidAPI pays
you out. Because of that, this endpoint needs its own auth check: RapidAPI
attaches a secret header (shown in your RapidAPI provider dashboard once
the API is added there) to every request it proxies, and a provider is
expected to reject anything missing or wrong for that header -- otherwise
someone could just call this URL directly and skip RapidAPI's billing
entirely.

This is deliberately a separate endpoint from /sentiment (x402) and
/v1/sentiment (this app's own Stripe tiers) rather than reusing either --
RapidAPI subscribers aren't in this app's own api_keys table at all, so
there's nothing here for verify_and_charge_api_key to check against; the
proxy secret is the only gate.

Env vars (see .env.example):
  RAPIDAPI_PROXY_SECRET  - from your RapidAPI provider dashboard once this
                            API is added there (Endpoints > Security). Set
                            it here to whatever RapidAPI shows you.
"""

import logging
import os

from fastapi import APIRouter, Header, HTTPException

from app.sentiment_service import compute_sentiment_payload, require_supported_symbol

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/rapidapi", tags=["rapidapi"])

RAPIDAPI_PROXY_SECRET = os.environ.get("RAPIDAPI_PROXY_SECRET", "")


# Only RapidAPI's proxy can call this (it sends the shared proxy secret), so
# it's declared as needing that header rather than as a free route.
RAPIDAPI_SCHEME = "RapidAPIProxySecret"


@router.get("/sentiment/{symbol}", openapi_extra={"security": [{RAPIDAPI_SCHEME: []}]})
async def rapidapi_sentiment(
    symbol: str,
    x_rapidapi_proxy_secret: str | None = Header(None, alias="X-RapidAPI-Proxy-Secret"),
    x_rapidapi_user: str | None = Header(None, alias="X-RapidAPI-User"),
    x_rapidapi_subscription: str | None = Header(None, alias="X-RapidAPI-Subscription"),
):
    if not RAPIDAPI_PROXY_SECRET:
        raise HTTPException(
            503,
            "This deployment hasn't been configured for RapidAPI yet "
            "(RAPIDAPI_PROXY_SECRET is unset). See BILLING.md / the "
            "RapidAPI listing setup.",
        )
    if x_rapidapi_proxy_secret != RAPIDAPI_PROXY_SECRET:
        raise HTTPException(
            403,
            "This endpoint only accepts traffic routed through RapidAPI. "
            "Use /sentiment/{symbol} (x402) or /v1/sentiment/{symbol} "
            "(API key) to call this API directly instead.",
        )

    symbol = require_supported_symbol(symbol, "rapidapi")
    try:
        payload = await compute_sentiment_payload(symbol)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    logger.info(
           "[rapidapi-usage] symbol=%s user=%s plan=%s",
           symbol.upper(),
           x_rapidapi_user or "unknown",
           x_rapidapi_subscription or "unknown",    
    )  
    return payload
