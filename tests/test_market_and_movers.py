"""
GET /market-sentiment (free) and GET /movers (x402) -- traction-focused
aggregate endpoints built on app/market.py.

Run with: python -m pytest
"""

import asyncio
import json
import tempfile
from datetime import datetime, timedelta, timezone

import os

os.environ.setdefault("PAY_TO_ADDRESS", "0x000000000000000000000000000000000000dEaD")
os.environ.setdefault("CDP_API_KEY_ID", "test")
os.environ.setdefault("CDP_API_KEY_SECRET", "test")
os.environ.setdefault("BILLING_DB_PATH", os.path.join(tempfile.mkdtemp(), "billing.db"))

import pytest
from fastapi.testclient import TestClient
from x402.schemas import SupportedKind, SupportedResponse

import app.billing as billing
from app import archive, hourly, main, market, window
from app import sentiment_service as svc
from app.billing import _db
from app.sources.news import Headline

UTC = timezone.utc
NOW = datetime(2026, 11, 9, 20, 0, 0, tzinfo=UTC)


@pytest.fixture
def db(monkeypatch, tmp_path):
    monkeypatch.setattr(billing, "DB_PATH", str(tmp_path / "market.db"))
    billing.init_db()
    hourly.init_hourly_db()


@pytest.fixture
def client(db, monkeypatch):
    monkeypatch.setattr(
        main.facilitator,
        "get_supported",
        lambda: SupportedResponse(kinds=[SupportedKind(x402Version=2, scheme="exact", network=main.CAIP2_NETWORK)]),
    )
    return TestClient(main.app)


def _insert_hourly(symbol, observed_at, compound, effective_n):
    with _db() as conn:
        conn.execute(
            "INSERT INTO sentiment_hourly (symbol, observed_at, average_compound, sample_size, "
            "fear_greed_value, matcher, drivers, effective_sample_size, unweighted_compound_72h, "
            'newest_headline_age_hours, "window") '
            "VALUES (?, ?, ?, 10, 50, 'whole_word', '[]', ?, ?, 1.0, ?)",
            (symbol, observed_at.isoformat(), compound, effective_n, compound, window.WINDOW_VERSION),
        )


def _payload(symbol, score, effective_n, label=None):
    """A minimal compute_sentiment_payloads()-shaped payload for one symbol."""
    if label is None:
        label = "bullish" if score >= window.BULLISH_AT else "bearish" if score <= window.BEARISH_AT else "neutral"
    return {
        "symbol": symbol,
        "overall_sentiment": {
            "label": label,
            "average_compound": score,
            "sample_size": 10,
            "effective_sample_size": effective_n,
        },
    }


# --- compute_market_index -----------------------------------------------

def test_market_index_excludes_insufficient_and_thin_coverage():
    payloads = {
        "BTC": _payload("BTC", 0.3, 40.0),
        "ETH": _payload("ETH", 0.1, 20.0),
        "DOGE": _payload("DOGE", 0.0, 0.0, label=window.INSUFFICIENT_LABEL),
        "SHIB": _payload("SHIB", 0.9, 1.0),  # below MIN_EFFECTIVE_SAMPLE
    }
    result = market.compute_market_index(payloads)
    assert result["coins_included"] == 2
    assert result["coins_considered"] == 4
    assert {c["symbol"] for c in result["contributors"]} == {"BTC", "ETH"}
    # Weighted toward BTC (40 vs 20): (0.3*40 + 0.1*20) / 60 = 0.2333
    assert result["market_sentiment"] == pytest.approx(0.2333, abs=1e-4)
    assert result["label"] == "bullish"


def test_market_index_all_insufficient_returns_null():
    payloads = {"DOGE": _payload("DOGE", 0.0, 0.0, label=window.INSUFFICIENT_LABEL)}
    result = market.compute_market_index(payloads)
    assert result["market_sentiment"] is None
    assert result["label"] == window.INSUFFICIENT_LABEL
    assert result["coins_included"] == 0


# --- compute_movers --------------------------------------------------------

def test_movers_ranks_by_shift_and_requires_both_sides_sufficient(db):
    current = {
        "BTC": _payload("BTC", 0.30, 40.0),   # prev 0.05 -> delta +0.25
        "ETH": _payload("ETH", -0.10, 30.0),  # prev 0.20 -> delta -0.30
        "SOL": _payload("SOL", 0.50, 1.0),    # current too thin -> excluded
        "ADA": _payload("ADA", 0.10, 30.0),   # no archive row ~24h ago -> excluded
    }
    _insert_hourly("BTC", NOW - timedelta(hours=24), 0.05, 20.0)
    _insert_hourly("ETH", NOW - timedelta(hours=24), 0.20, 20.0)
    _insert_hourly("SOL", NOW - timedelta(hours=24), 0.05, 20.0)
    _insert_hourly("ADA", NOW - timedelta(hours=50), 0.00, 20.0)  # way outside tolerance

    result = market.compute_movers(current, limit=10, now=NOW)
    assert result["coins_ranked"] == 2
    assert result["coins_considered"] == 4
    assert result["movers_up"][0]["symbol"] == "BTC"
    assert result["movers_up"][0]["delta"] == pytest.approx(0.25, abs=1e-4)
    # Only 2 coins ranked and limit=10 -> no separate movers_down tail.
    assert result["movers_down"] == []


def test_movers_down_tail_when_more_coins_than_limit(db):
    current = {sym: _payload(sym, score, 30.0) for sym, score in (
        ("A", 0.30), ("B", 0.20), ("C", 0.10), ("D", -0.10), ("E", -0.20), ("F", -0.30),
    )}
    for sym in current:
        _insert_hourly(sym, NOW - timedelta(hours=24), 0.0, 20.0)

    result = market.compute_movers(current, limit=2, now=NOW)
    assert [c["symbol"] for c in result["movers_up"]] == ["A", "B"]
    assert [c["symbol"] for c in result["movers_down"]] == ["F", "E"]


# --- live routes -------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_market_cache():
    # Every test starts from a clean cache so one test's refresh doesn't
    # leak into the next.
    market._market_cache["computed_at"] = None
    market._market_cache["result"] = None
    yield
    market._market_cache["computed_at"] = None
    market._market_cache["result"] = None


def test_get_market_sentiment_is_free_and_unauthenticated(client, monkeypatch):
    async def fake_payloads(symbols):
        return {
            "BTC": _payload("BTC", 0.3, 40.0),
            "ETH": _payload("ETH", 0.1, 20.0),
        }

    monkeypatch.setattr(market, "compute_sentiment_payloads", fake_payloads)
    r = client.get("/market-sentiment")
    assert r.status_code == 200
    body = r.json()
    assert body["coins_included"] == 2
    assert "contributors" in body
    assert body["cached"] is False
    assert body["computed_at"]
    assert body["next_refresh_at"]


def test_get_movers_is_x402_gated(client):
    r = client.get("/movers")
    assert r.status_code == 402
    assert r.headers.get("payment-required")


def test_movers_in_x402_routes_table():
    assert "GET /movers" in main.routes
    assert main.routes["GET /movers"].accepts[0].price == market.MOVERS_PRICE_USD


# --- /market-sentiment caching ------------------------------------------

def test_market_sentiment_is_cached_within_ttl(monkeypatch):
    calls = []

    async def counting_payloads(symbols):
        calls.append(list(symbols))
        return {"BTC": _payload("BTC", 0.3, 40.0)}

    monkeypatch.setattr(market, "compute_sentiment_payloads", counting_payloads)

    t0 = NOW
    first = asyncio.run(market.compute_market_sentiment(now=t0))
    assert len(calls) == 1
    assert first["cached"] is False

    # A second call well within the TTL reuses the cached result -- no
    # second feed fetch.
    t1 = t0 + timedelta(minutes=5)
    second = asyncio.run(market.compute_market_sentiment(now=t1))
    assert len(calls) == 1
    assert second["cached"] is True
    assert second["market_sentiment"] == first["market_sentiment"]
    assert second["computed_at"] == first["computed_at"]


def test_market_sentiment_refreshes_after_ttl(monkeypatch):
    calls = []

    async def counting_payloads(symbols):
        calls.append(list(symbols))
        return {"BTC": _payload("BTC", 0.3, 40.0)}

    monkeypatch.setattr(market, "compute_sentiment_payloads", counting_payloads)

    t0 = NOW
    asyncio.run(market.compute_market_sentiment(now=t0))
    assert len(calls) == 1

    t1 = t0 + timedelta(seconds=market.MARKET_SENTIMENT_CACHE_SECONDS + 1)
    refreshed = asyncio.run(market.compute_market_sentiment(now=t1))
    assert len(calls) == 2
    assert refreshed["cached"] is False
    assert refreshed["computed_at"] != NOW.isoformat()


def test_market_sentiment_custom_symbols_bypasses_cache(monkeypatch):
    calls = []

    async def counting_payloads(symbols):
        calls.append(list(symbols))
        return {"BTC": _payload("BTC", 0.3, 40.0)}

    monkeypatch.setattr(market, "compute_sentiment_payloads", counting_payloads)

    asyncio.run(market.compute_market_sentiment(symbols=["BTC"], now=NOW))
    asyncio.run(market.compute_market_sentiment(symbols=["BTC"], now=NOW))
    assert len(calls) == 2  # never cached -- each call is live
