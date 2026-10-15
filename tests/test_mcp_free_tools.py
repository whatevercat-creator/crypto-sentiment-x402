"""The MCP package's free tools and its no-wallet start. No network: every
HTTP call goes through httpx.MockTransport."""

import asyncio
import base64
import json

import httpx
import pytest

import crypto_sentiment_x402_mcp as mcp_pkg
from app.coins import COIN_NAMES

PAY_TO = "0x1111111111111111111111111111111111111111"
CHALLENGE = {
    "x402Version": 2,
    "error": "Payment required",
    "accepts": [
        {
            "scheme": "exact",
            "network": "eip155:8453",
            "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            "amount": "10000",
            "payTo": PAY_TO,
            "maxTimeoutSeconds": 300,
            "extra": {"name": "USD Coin", "version": "2"},
        }
    ],
}


@pytest.fixture
def mock_http(monkeypatch):
    """Route the package's httpx.AsyncClient through a handler; record requests."""
    calls = []
    state = {"handler": None}
    real_client = httpx.AsyncClient

    def handler(request):
        calls.append(request)
        return state["handler"](request)

    def client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(mcp_pkg.httpx, "AsyncClient", client)
    return state, calls


def test_no_cdp_vars_returns_friendly_message(monkeypatch):
    monkeypatch.delenv("CDP_API_KEY_ID", raising=False)
    monkeypatch.delenv("CDP_API_KEY_SECRET", raising=False)

    def no_wallet():
        raise AssertionError("must not create a wallet without CDP vars")

    monkeypatch.setattr(mcp_pkg, "_get_payment_client", no_wallet)
    out = asyncio.run(mcp_pkg.crypto_sentiment("BTC"))

    assert "https://portal.cdp.coinbase.com" in out["detail"]
    assert "funded CDP wallet" in out["detail"]
    assert out["free_tools"] == ["list_supported_coins", "archive_coverage", "price_quote"]


def test_list_supported_coins_matches_app():
    assert mcp_pkg.SUPPORTED_COINS == COIN_NAMES
    assert list(mcp_pkg.SUPPORTED_COINS) == list(COIN_NAMES)
    out = asyncio.run(mcp_pkg.list_supported_coins())
    assert out["count"] == 24
    assert [c["symbol"] for c in out["coins"]] == list(COIN_NAMES)


def test_price_quote_unsupported_symbol_makes_no_http_call(mock_http):
    state, calls = mock_http
    state["handler"] = lambda r: httpx.Response(500)
    out = asyncio.run(mcp_pkg.price_quote("notacoin"))
    assert calls == []
    assert out["supported"] is False
    assert "not charged" in out["detail"]


def test_price_quote_parses_402_header(mock_http):
    state, calls = mock_http
    header = base64.b64encode(json.dumps(CHALLENGE).encode()).decode()
    state["handler"] = lambda r: httpx.Response(
        402, headers={"payment-required": header}, json={}
    )
    out = asyncio.run(mcp_pkg.price_quote(" btc "))

    assert len(calls) == 1
    assert calls[0].url.path == "/sentiment/BTC"
    assert "payment-signature" not in calls[0].headers
    assert "x-payment" not in calls[0].headers
    assert out["price_usd"] == 0.01
    assert out["network"] == "eip155:8453"
    assert out["asset"] == CHALLENGE["accepts"][0]["asset"]
    assert out["payTo"] == PAY_TO
    assert "nothing was paid" in out["note"].lower()


def test_price_quote_falls_back_to_402_body(mock_http):
    state, _ = mock_http
    state["handler"] = lambda r: httpx.Response(402, json=CHALLENGE)
    out = asyncio.run(mcp_pkg.price_quote("ETH"))
    assert out["payTo"] == PAY_TO
    assert out["price_usd"] == 0.01


def test_archive_coverage_passes_json_through(mock_http):
    state, calls = mock_http
    summary = {"as_of": "2026-10-15T00:00:00Z", "symbols": [{"symbol": "BTC", "rows": 5}]}
    state["handler"] = lambda r: httpx.Response(200, json=summary)
    out = asyncio.run(mcp_pkg.archive_coverage())

    assert out == summary
    assert calls[0].url.path == "/archive"
    assert calls[0].headers["accept"] == "application/json"
