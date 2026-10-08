"""
We don't charge for answers we don't have: a reading for a supported coin
that comes out as window.INSUFFICIENT_LABEL is refused with a 422 and no
reading on every sold lane. x402 never settles it, the subscription lane
doesn't count it, and each refusal logs one "insufficient_news_refused" line.
"""

import json
import logging
import secrets
from datetime import datetime, timedelta, timezone

import pytest

from tests.test_home import client  # noqa: F401  (shared fixture)
from tests.test_paid_call_log import _paid_call_lines, _payment_header, paid  # noqa: F401
from app import main, rapidapi
from app import sentiment_service as svc
from app.billing import _db, _now_iso
from app.sources.news import Headline

QUIET_MESSAGE = (
    "Not enough recent news for LINK in the last 72 hours to give a reliable "
    "reading (effective sample size 2.0, need 5). Not charged. Try BTC or ETH, "
    "or LINK again later."
)


def _headlines(n):
    published = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return [
        Headline(title=f"Headline {i} is good news", source="CoinDesk",
                 link=f"https://x.test/{i}", published=published, text=f"Headline {i} is good news")
        for i in range(n)
    ]


async def fake_payload(symbol):
    """LINK is quiet (2 equal-weight headlines, effective n 2.0); anything else is busy (8)."""
    symbol = symbol.upper()
    return svc.build_payload(symbol, _headlines(2 if symbol == "LINK" else 8), None)


def _refusal_lines(caplog):
    lines = []
    for record in caplog.records:
        try:
            data = json.loads(record.getMessage())
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("event") == "insufficient_news_refused":
            lines.append(data)
    return lines


def _assert_one_refusal(caplog, lane):
    [line] = _refusal_lines(caplog)
    assert set(line) == {"event", "ts", "symbol", "lane", "effective_n"}
    assert line["symbol"] == "LINK" and line["lane"] == lane and line["effective_n"] == 2.0


def test_fixture_payloads_are_quiet_and_busy():
    import asyncio
    assert asyncio.run(fake_payload("LINK"))["overall_sentiment"]["label"] == svc.window.INSUFFICIENT_LABEL
    assert asyncio.run(fake_payload("BTC"))["overall_sentiment"]["label"] != svc.window.INSUFFICIENT_LABEL


def test_paid_call_for_quiet_coin_is_422_and_never_settled(paid, caplog, monkeypatch):  # noqa: F811
    client, _ = paid
    monkeypatch.setattr(main, "compute_sentiment_payload", fake_payload)
    settles = []
    real_settle = main.facilitator.settle

    async def counting_settle(payload, requirements):
        settles.append(1)
        return await real_settle(payload, requirements)

    monkeypatch.setattr(main.facilitator, "settle", counting_settle)
    header = _payment_header(client, "LINK")
    with caplog.at_level(logging.INFO):
        r = client.get("/sentiment/LINK", headers={"PAYMENT-SIGNATURE": header})
    assert r.status_code == 422
    assert r.json() == {"detail": QUIET_MESSAGE}
    assert settles == []
    assert _paid_call_lines(caplog) == []
    _assert_one_refusal(caplog, "x402")

    # A busy coin still gets its reading and settles once.
    header = _payment_header(client, "BTC")
    r = client.get("/sentiment/BTC", headers={"PAYMENT-SIGNATURE": header})
    assert r.status_code == 200 and settles == [1]
    assert r.json()["overall_sentiment"]["label"] != svc.window.INSUFFICIENT_LABEL


@pytest.fixture
def key(client):  # noqa: F811
    api_key = "csk_test_" + secrets.token_hex(8)
    now = _now_iso()
    with _db() as conn:
        conn.execute(
            "INSERT INTO api_keys (api_key, email, tier, status, period_calls_used, period_start, created_at) "
            "VALUES (?, ?, 'pro', 'active', 0, ?, ?)",
            (api_key, "pro@example.com", now, now),
        )
    return api_key


def _calls_used(api_key):
    with _db() as conn:
        return conn.execute("SELECT period_calls_used FROM api_keys WHERE api_key = ?", (api_key,)).fetchone()[0]


def test_subscription_call_for_quiet_coin_is_not_counted(client, key, caplog, monkeypatch):  # noqa: F811
    monkeypatch.setattr(main, "compute_sentiment_payload", fake_payload)
    with caplog.at_level(logging.INFO):
        r = client.get("/v1/sentiment/LINK", headers={"X-API-Key": key})
    assert r.status_code == 422 and r.json() == {"detail": QUIET_MESSAGE}
    assert _calls_used(key) == 0
    _assert_one_refusal(caplog, "v1")

    r = client.get("/v1/sentiment/BTC", headers={"X-API-Key": key})
    assert r.status_code == 200
    assert _calls_used(key) == 1 and r.json()["_billing"]["calls_used_this_period"] == 1


@pytest.mark.parametrize("status, expected", [(None, 401), ("canceled", 403)])
def test_subscription_key_is_still_checked_before_any_work(client, key, monkeypatch, status, expected):  # noqa: F811
    computed = []

    async def tracking_payload(symbol):
        computed.append(symbol)
        return await fake_payload(symbol)

    monkeypatch.setattr(main, "compute_sentiment_payload", tracking_payload)
    if status is None:
        api_key = "csk_test_unknown"
    else:
        api_key = key
        with _db() as conn:
            conn.execute("UPDATE api_keys SET status = ? WHERE api_key = ?", (status, key))
    r = client.get("/v1/sentiment/BTC", headers={"X-API-Key": api_key})
    assert r.status_code == expected and computed == []


def test_rapidapi_call_for_quiet_coin_is_422(client, caplog, monkeypatch):  # noqa: F811
    monkeypatch.setattr(rapidapi, "RAPIDAPI_PROXY_SECRET", "proxy-secret")
    monkeypatch.setattr(rapidapi, "compute_sentiment_payload", fake_payload)
    headers = {"X-RapidAPI-Proxy-Secret": "proxy-secret"}
    with caplog.at_level(logging.INFO):
        r = client.get("/rapidapi/sentiment/LINK", headers=headers)
    assert r.status_code == 422 and r.json() == {"detail": QUIET_MESSAGE}
    _assert_one_refusal(caplog, "rapidapi")

    assert client.get("/rapidapi/sentiment/BTC", headers=headers).status_code == 200


def test_openapi_and_llms_document_the_422(client):  # noqa: F811
    spec = client.get("/openapi.json").json()
    for path in ("/sentiment/{symbol}", "/v1/sentiment/{symbol}", "/rapidapi/sentiment/{symbol}"):
        description = spec["paths"][path]["get"]["responses"]["422"]["description"]
        assert "Not enough recent news" in description and "Not charged" in description
    text = client.get("/llms.txt").text
    assert "HTTP 422" in text and "not charged" in text
