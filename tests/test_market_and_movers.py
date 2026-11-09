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
    # Direction decides the list: ETH fell, so it's in movers_down only.
    assert [c["symbol"] for c in result["movers_up"]] == ["BTC"]
    assert [c["symbol"] for c in result["movers_down"]] == ["ETH"]


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
    market._market_cache["payloads"] = None
    yield
    market._market_cache["computed_at"] = None
    market._market_cache["payloads"] = None


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


# --- /movers reads the shared hourly cache ------------------------------

def _counting_fetch(monkeypatch):
    """Patch the one live fetch (compute_sentiment_payloads = one feed fetch
    for all coins) with a counter; returns the list of calls."""
    calls = []

    async def counting_payloads(symbols):
        calls.append(list(symbols))
        return {"BTC": _payload("BTC", 0.30, 40.0), "ETH": _payload("ETH", -0.10, 30.0)}

    monkeypatch.setattr(market, "compute_sentiment_payloads", counting_payloads)
    return calls


def test_two_movers_calls_within_ttl_cause_one_feed_fetch(db, monkeypatch):
    # Calls the route handler directly (past the x402 paywall), as
    # test_archive.py does for /history. limit is passed explicitly because
    # its default is a FastAPI Query object. One archived row ~24h ago so a
    # coin is ranked (with none ranked, /movers refuses with 422).
    calls = _counting_fetch(monkeypatch)
    _insert_hourly("BTC", datetime.now(UTC) - timedelta(hours=24), 0.05, 20.0)
    first =json.loads(asyncio.run(main.get_movers(limit=10)).body)
    second = json.loads(asyncio.run(main.get_movers(limit=10)).body)
    assert len(calls) == 1
    assert first["computed_at"] == second["computed_at"]
    assert first["computed_at"]


def test_movers_refetches_after_ttl(db, monkeypatch):
    calls = _counting_fetch(monkeypatch)
    asyncio.run(market.compute_movers_cached(now=NOW))
    asyncio.run(market.compute_movers_cached(now=NOW + timedelta(minutes=59)))
    assert len(calls) == 1
    later = asyncio.run(market.compute_movers_cached(
        now=NOW + timedelta(seconds=market.MARKET_SENTIMENT_CACHE_SECONDS + 1)))
    assert len(calls) == 2
    assert later["computed_at"] != NOW.isoformat()


def test_movers_and_market_sentiment_share_one_fetch(db, monkeypatch):
    calls = _counting_fetch(monkeypatch)
    asyncio.run(market.compute_market_sentiment(now=NOW))
    asyncio.run(market.compute_movers_cached(now=NOW + timedelta(minutes=10)))
    assert len(calls) == 1


def test_movers_lookback_is_measured_from_computed_at(db, monkeypatch):
    # The cached "now" reading was taken at NOW; a call 50 minutes later must
    # compare it with the row ~24h before NOW (not before the call time) and
    # report computed_at = NOW.
    _counting_fetch(monkeypatch)
    _insert_hourly("BTC", NOW - timedelta(hours=24), 0.05, 20.0)
    _insert_hourly("BTC", NOW - timedelta(hours=23), 0.25, 20.0)
    asyncio.run(market.compute_movers_cached(now=NOW))
    result = asyncio.run(market.compute_movers_cached(now=NOW + timedelta(minutes=50)))
    assert result["computed_at"] == NOW.isoformat()
    btc = next(c for c in result["movers_up"] if c["symbol"] == "BTC")
    assert btc["previous_score"] == pytest.approx(0.05)
    assert btc["delta"] == pytest.approx(0.25, abs=1e-4)


def test_movers_402_bazaar_metadata(client):
    import base64

    r = client.get("/movers")
    assert r.status_code == 402
    required = json.loads(base64.b64decode(r.headers["payment-required"]))
    resource = required["resource"]
    assert resource["description"].startswith("Crypto sentiment movers")
    assert "Use when" in resource["description"]
    assert len(resource["description"]) < 500
    assert resource["serviceName"] == main.DISCOVERY_SERVICE_NAME
    assert resource["tags"] == main.DISCOVERY_TAGS
    assert required["accepts"][0]["amount"] == "20000"
    assert required["accepts"][0]["payTo"] == main.PAY_TO_ADDRESS


# --- /movers refuses (422, not charged) when no coin can be ranked -----------

from tests.test_paid_call_log import PAYER, SIGNATURE, paid  # noqa: E402,F401


def _movers_payment_header(client):
    import base64

    challenge = json.loads(base64.b64decode(client.get("/movers").headers["payment-required"]))
    accepted = challenge["accepts"][0]
    payload = {
        "x402Version": 2,
        "resource": challenge["resource"],
        "accepted": accepted,
        "payload": {
            "signature": SIGNATURE,
            "authorization": {
                "from": PAYER,
                "to": accepted["payTo"],
                "value": accepted["amount"],
                "validAfter": "0",
                "validBefore": "9999999999",
                "nonce": "0x" + "00" * 32,
            },
        },
    }
    return base64.b64encode(json.dumps(payload).encode()).decode()


def test_paid_movers_with_nothing_rankable_is_422_and_never_settled(paid, monkeypatch, caplog):  # noqa: F811
    import logging

    client, _ = paid
    _counting_fetch(monkeypatch)  # BTC and ETH have enough news now...
    settles = []
    real_settle = main.facilitator.settle

    async def counting_settle(payload, requirements):
        settles.append(1)
        return await real_settle(payload, requirements)

    monkeypatch.setattr(main.facilitator, "settle", counting_settle)

    # ...but the archive is empty, so no coin has a ~24h-ago reading.
    with caplog.at_level(logging.INFO, logger="app.main"):
        r = client.get("/movers", headers={"PAYMENT-SIGNATURE": _movers_payment_header(client)})
    assert r.status_code == 422
    assert "Not charged." in r.json()["detail"]
    assert settles == []
    assert "movers_refused" in caplog.text

    # Once a coin can be ranked, the same paid call returns 200 and settles once.
    _insert_hourly("BTC", datetime.now(UTC) - timedelta(hours=24), 0.05, 20.0)
    r = client.get("/movers", headers={"PAYMENT-SIGNATURE": _movers_payment_header(client)})
    assert r.status_code == 200
    assert r.json()["coins_ranked"] == 1
    assert settles == [1]


def test_movers_description_says_refused_and_not_charged():
    d = main.MOVERS_ROUTE_DESCRIPTION
    assert "If no coin can be ranked, the call is refused and not charged." in d
    assert len(d) < 500


# --- /movers reads only the archive rows around ~24h ago ---------------------

def test_movers_reads_only_a_window_of_the_archive(db, monkeypatch):
    seen = []
    real_hourly_rows = archive.hourly_rows

    def spy(*args, **kwargs):
        seen.append(kwargs)
        return real_hourly_rows(*args, **kwargs)

    monkeypatch.setattr(market.archive_module, "hourly_rows", spy)
    _insert_hourly("BTC", NOW - timedelta(hours=24), 0.05, 20.0)   # in window
    _insert_hourly("BTC", NOW - timedelta(hours=20), 0.10, 20.0)   # edge: exactly +4h, still in
    _insert_hourly("BTC", NOW - timedelta(hours=40), 0.90, 20.0)   # outside: never returned
    result = market.compute_movers({"BTC": _payload("BTC", 0.30, 40.0)}, now=NOW)

    [kwargs] = seen
    target = NOW - market.MOVERS_LOOKBACK
    assert kwargs["start"] == target - market.MOVERS_LOOKBACK_TOLERANCE
    assert kwargs["end"] > target + market.MOVERS_LOOKBACK_TOLERANCE
    assert kwargs["end"] - (target + market.MOVERS_LOOKBACK_TOLERANCE) <= timedelta(seconds=1)
    returned = real_hourly_rows(**kwargs)
    assert {r["observed_at"] for r in returned} == {
        (NOW - timedelta(hours=24)).isoformat(), (NOW - timedelta(hours=20)).isoformat()}
    assert result["movers_up"][0]["previous_score"] == pytest.approx(0.05)


# --- movers_up only rises, movers_down only falls ----------------------------

def _ranked(deltas, limit=10):
    """compute_movers for {symbol: delta}: each coin was 0.0 ~24h ago and is
    `delta` now, both with plenty of news."""
    current = {sym: _payload(sym, delta, 30.0) for sym, delta in deltas.items()}
    for sym in current:
        _insert_hourly(sym, NOW - timedelta(hours=24), 0.0, 20.0)
    return market.compute_movers(current, limit=limit, now=NOW)


def _symbols(entries):
    return [e["symbol"] for e in entries]


def test_movers_all_rising(db):
    result = _ranked({"A": 0.10, "B": 0.30, "C": 0.20}, limit=2)
    assert _symbols(result["movers_up"]) == ["B", "C"]  # largest rise first, capped at 2
    assert result["movers_down"] == []
    assert result["coins_ranked"] == 3


def test_movers_all_falling(db):
    result = _ranked({"A": -0.10, "B": -0.30, "C": -0.20}, limit=2)
    assert result["movers_up"] == []
    assert _symbols(result["movers_down"]) == ["B", "C"]  # largest fall first
    assert result["coins_ranked"] == 3


def test_movers_mixed_with_fewer_coins_than_limit(db):
    # 4 coins, limit 10: a falling coin must never land in movers_up.
    result = _ranked({"A": 0.25, "B": 0.05, "C": -0.30, "D": -0.10}, limit=10)
    assert _symbols(result["movers_up"]) == ["A", "B"]
    assert _symbols(result["movers_down"]) == ["C", "D"]
    assert all(e["delta"] > 0 for e in result["movers_up"])
    assert all(e["delta"] < 0 for e in result["movers_down"])


def test_movers_zero_delta_in_neither_list(db):
    result = _ranked({"A": 0.20, "Z": 0.0, "C": -0.20}, limit=10)
    assert _symbols(result["movers_up"]) == ["A"]
    assert _symbols(result["movers_down"]) == ["C"]
    assert result["coins_ranked"] == 3  # Z was ranked, just didn't move


# --- paid /movers calls are logged like /sentiment and /history ---------------

def _movers_paid_lines(caplog):
    lines = []
    for record in caplog.records:
        try:
            data = json.loads(record.getMessage())
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("event") == "movers_paid_call":
            lines.append(data)
    return lines


def test_paid_movers_call_logs_one_line_and_refusal_logs_none(paid, monkeypatch, caplog):  # noqa: F811
    import logging
    from tests.test_paid_call_log import TX

    client, _ = paid
    _counting_fetch(monkeypatch)

    # Nothing rankable (empty archive): 422, not settled, no paid-call line.
    with caplog.at_level(logging.INFO, logger="app.main"):
        r = client.get("/movers", headers={"PAYMENT-SIGNATURE": _movers_payment_header(client)})
    assert r.status_code == 422
    assert _movers_paid_lines(caplog) == []

    # Rankable: 200, settled, exactly one line with the same keys as the
    # other routes' paid-call lines (symbol null: /movers has none).
    _insert_hourly("BTC", datetime.now(UTC) - timedelta(hours=24), 0.05, 20.0)
    caplog.clear()
    header = _movers_payment_header(client)
    with caplog.at_level(logging.INFO, logger="app.main"):
        r = client.get("/movers", headers={"PAYMENT-SIGNATURE": header})
    assert r.status_code == 200
    [line] = _movers_paid_lines(caplog)
    assert set(line) == {"event", "ts", "symbol", "price", "payer", "transaction"}
    assert line["symbol"] is None
    assert line["price"] == market.MOVERS_PRICE_USD
    assert line["payer"] == PAYER
    assert line["transaction"] == TX
    assert SIGNATURE not in caplog.text and header not in caplog.text
    # And it isn't mistaken for a /sentiment or /history call.
    assert '"sentiment_paid_call"' not in caplog.text and '"history_paid_call"' not in caplog.text
