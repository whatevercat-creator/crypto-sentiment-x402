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
    assert out["charged"] is False
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


# --- 0.1.4: symbol enums, get_hourly_history, version at initialize ---

import contextlib
import tomllib
from pathlib import Path

from mcp import Client

ROOT = Path(__file__).resolve().parent.parent


def _tool_schemas():
    async def go():
        async with Client(mcp_pkg.mcp) as client:
            return {t.name: t.input_schema for t in (await client.list_tools()).tools}

    return asyncio.run(go())


@pytest.mark.parametrize("tool", ["crypto_sentiment", "price_quote"])
def test_symbol_schema_is_enum_of_supported_coins(tool):
    prop = _tool_schemas()[tool]["properties"]["symbol"]
    assert prop["enum"] == list(mcp_pkg.SUPPORTED_COINS)
    assert prop["description"].startswith("Uppercase crypto ticker, one of the 24")
    assert "refused and not charged" in prop["description"]


def test_hourly_history_schema():
    schema = _tool_schemas()["get_hourly_history"]
    assert schema["required"] == ["symbol"]
    for name in ("start", "end"):
        assert "ISO 8601" in schema["properties"][name]["description"]


def test_hourly_history_without_cdp_vars_returns_friendly_message(monkeypatch):
    monkeypatch.delenv("CDP_API_KEY_ID", raising=False)
    monkeypatch.delenv("CDP_API_KEY_SECRET", raising=False)

    def no_wallet():
        raise AssertionError("must not create a wallet without CDP vars")

    monkeypatch.setattr(mcp_pkg, "_get_payment_client", no_wallet)
    out = asyncio.run(mcp_pkg.get_hourly_history("BTC"))

    assert out["error"] == "no wallet configured"
    assert "https://portal.cdp.coinbase.com" in out["detail"]
    assert out["charged"] is False
    assert out["free_tools"] == ["list_supported_coins", "archive_coverage", "price_quote"]


@pytest.fixture
def mock_paid_http(monkeypatch):
    """Fake CDP vars and route the x402 client through a MockTransport."""
    monkeypatch.setenv("CDP_API_KEY_ID", "test-id")
    monkeypatch.setenv("CDP_API_KEY_SECRET", "test-secret")
    class Calls(list):
        state: dict

    calls = Calls()
    state = {"response": lambda r: httpx.Response(200, json={"symbol": "BTC", "readings": []})}

    async def fake_payment_client():
        return object()

    @contextlib.asynccontextmanager
    async def fake_x402_client(payment_client):
        def handler(request):
            calls.append(request)
            return state["response"](request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            yield http

    monkeypatch.setattr(mcp_pkg, "_get_payment_client", fake_payment_client)
    monkeypatch.setattr(mcp_pkg, "x402HttpxClient", fake_x402_client)
    calls.state = state
    return calls


def test_hourly_history_url_without_range(mock_paid_http):
    out = asyncio.run(mcp_pkg.get_hourly_history(" btc "))
    assert out == {"symbol": "BTC", "readings": []}
    assert len(mock_paid_http) == 1
    url = mock_paid_http[0].url
    assert str(url) == f"{mcp_pkg.API_URL}/history/BTC"
    assert url.query == b""


def test_hourly_history_url_with_range(mock_paid_http):
    asyncio.run(
        mcp_pkg.get_hourly_history(
            "ETH", start="2026-10-13T00:00:00Z", end="2026-10-14T00:00:00Z"
        )
    )
    url = mock_paid_http[0].url
    assert url.path == "/history/ETH"
    assert dict(url.params) == {
        "start": "2026-10-13T00:00:00Z",
        "end": "2026-10-14T00:00:00Z",
    }


def test_hourly_history_url_with_start_only(mock_paid_http):
    asyncio.run(mcp_pkg.get_hourly_history("SOL", start="2026-10-13T00:00:00Z"))
    assert dict(mock_paid_http[0].url.params) == {"start": "2026-10-13T00:00:00Z"}


def test_version_at_initialize_matches_pyproject():
    expected = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]

    async def go():
        async with Client(mcp_pkg.mcp) as client:
            return client.server_info

    info = asyncio.run(go())
    assert info.name == "crypto-sentiment"
    assert info.version == expected == mcp_pkg.__version__


def test_hourly_history_passes_no_readings_404_through(mock_paid_http):
    body = {
        "error": "no_readings_in_range",
        "symbol": "SOL",
        "start": "2026-10-01T00:00:00Z",
        "end": "2026-10-10T00:00:00Z",
        "first_reading": "2026-10-12T07:00:00Z",
        "message": "No hourly readings for SOL in this range, so nothing was charged.",
    }
    mock_paid_http.state["response"] = lambda r: httpx.Response(404, json=body)
    out = asyncio.run(
        mcp_pkg.get_hourly_history(
            "SOL", start="2026-10-01T00:00:00Z", end="2026-10-10T00:00:00Z"
        )
    )
    assert out["error"] == "API returned HTTP 404"
    assert out["detail"] == body
    assert out["charged"] is False
    assert "nothing was charged" in out["note"]
    assert "paying_wallet" not in out


def test_paid_402_keeps_wallet_hint(mock_paid_http):
    mock_paid_http.state["response"] = lambda r: httpx.Response(402, json={})
    out = asyncio.run(mcp_pkg.get_hourly_history("BTC"))
    assert out["error"] == "API returned HTTP 402"
    assert "paying_wallet" in out and "USDC" in out["hint"]
