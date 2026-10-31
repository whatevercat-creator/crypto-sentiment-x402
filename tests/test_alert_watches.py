"""
POST /alerts/watch input checks, /alerts/history errors, and watches only
firing while their key is active.
"""

import asyncio

import pytest
from fastapi.testclient import TestClient

import tests.test_home  # noqa: F401
from app import alerts, billing, integrations, main
from app.billing import _db, _now_iso

KEY = "csk_alerts_test"
HEADERS = {"X-API-Key": KEY}
GOOD = {"symbol": "BTC", "channel_type": "webhook", "channel_target": "https://hooks.zapier.com/x"}


def _key(key=KEY, status="active"):
    with _db() as conn:
        conn.execute(
            "INSERT INTO api_keys (api_key, tier, status, period_start, created_at) VALUES (?, 'pro', ?, ?, ?)",
            (key, status, _now_iso(), _now_iso()),
        )


def _watch(key=KEY, last_label="bullish", last_compound=0.5):
    with _db() as conn:
        return conn.execute(
            "INSERT INTO watches (api_key, symbol, channel_type, channel_target, threshold, last_label, "
            "last_compound, created_at) VALUES (?, 'BTC', 'webhook', 'https://hook.customer.dev/x', 0.3, ?, ?, ?)",
            (key, last_label, last_compound, _now_iso()),
        ).lastrowid


@pytest.fixture
def api(monkeypatch, tmp_path):
    monkeypatch.setattr(billing, "DB_PATH", str(tmp_path / "a.db"))
    billing.init_db()
    alerts.init_alerts_db()
    _key()
    return TestClient(main.app)


def _watch_count():
    with _db() as conn:
        return conn.execute("SELECT COUNT(*) FROM watches").fetchone()[0]


def test_good_watch_created(api):
    r = api.post("/alerts/watch", json={**GOOD, "threshold": 2}, headers=HEADERS)
    assert r.status_code == 200 and r.json()["threshold"] == 2


def test_default_threshold_is_valid(api):
    assert api.post("/alerts/watch", json=GOOD, headers=HEADERS).json()["threshold"] == 0.3


@pytest.mark.parametrize("threshold", ["0", "-0.1", "2.01", "1e308", "NaN", "Infinity", "-Infinity"])
def test_threshold_out_of_range_refused(api, threshold):
    body = '{"symbol": "BTC", "channel_type": "webhook", ' \
           f'"channel_target": "https://hooks.zapier.com/x", "threshold": {threshold}}}'
    r = api.post("/alerts/watch", content=body, headers={**HEADERS, "Content-Type": "application/json"})
    assert r.status_code == 400 and "threshold" in r.json()["detail"]
    assert _watch_count() == 0


@pytest.mark.parametrize("channel,target", [
    ("webhook", "http://hooks.zapier.com/x"),
    ("webhook", "https://169.254.169.254/x"),
    ("webhook", "https://[::1]/x"),
    ("webhook", "https://[fd00::1]/x"),
    ("webhook", "https://a.dev/" + "a" * 2048),
    ("discord", "https://hooks.zapier.com/x"),
])
def test_bad_target_refused_at_creation(api, channel, target):
    r = api.post("/alerts/watch", json={**GOOD, "channel_type": channel, "channel_target": target}, headers=HEADERS)
    assert r.status_code == 400
    assert "channel_target" in r.json()["detail"] or "Discord" in r.json()["detail"]
    assert target not in r.text
    assert _watch_count() == 0


def test_history_hides_old_raw_errors(api):
    watch_id = _watch()
    raw = "Client error for url 'https://api.telegram.org/bot123:SECRET/sendMessage'"
    with _db() as conn:
        for error in (raw, "delivery failed: HTTP 404", "delivery failed: timeout", "", None):
            conn.execute(
                "INSERT INTO alert_events (watch_id, symbol, delivered, error, sent_at) VALUES (?, 'BTC', 0, ?, ?)",
                (watch_id, error, _now_iso()),
            )
    r = api.get("/alerts/history", headers=HEADERS)
    assert "SECRET" not in r.text
    assert sorted(e["error"] or "" for e in r.json()["events"]) == [
        "", "", "delivery failed", "delivery failed: HTTP 404", "delivery failed: timeout",
    ]


@pytest.mark.parametrize("error", [
    "delivery failed: HTTP 404 https://secret", "xdelivery failed: timeout",
])
def test_public_error_needs_an_exact_match(error):
    assert alerts._public_error(error) == "delivery failed"


def _poll(monkeypatch):
    sent = []

    async def payload(symbol):
        return {"overall_sentiment": {"label": "bearish", "average_compound": -0.5}}

    async def deliver(watch, message, event):
        sent.append(watch["api_key"])
        return True, ""

    monkeypatch.setattr(alerts, "compute_sentiment_payload", payload)
    monkeypatch.setattr(alerts, "_deliver", deliver)
    asyncio.run(alerts._poll_once())
    return sent


def test_poller_only_fires_watches_of_active_keys(api, monkeypatch):
    _key("csk_canceled", "canceled")
    _watch()
    _watch("csk_canceled")
    _watch("csk_key_row_gone")
    assert _poll(monkeypatch) == [KEY]


def test_poller_skips_a_symbol_only_inactive_keys_watch(api, monkeypatch):
    _key("csk_canceled", "canceled")
    _watch("csk_canceled")

    async def payload(symbol):
        raise AssertionError("no work for a symbol nobody active watches")

    monkeypatch.setattr(alerts, "compute_sentiment_payload", payload)
    asyncio.run(alerts._poll_once())


def test_watch_resumes_when_key_recovers(api, monkeypatch):
    _key("csk_lapsed", "canceled")
    _watch("csk_lapsed")
    assert _poll(monkeypatch) == []
    with _db() as conn:
        conn.execute("UPDATE api_keys SET status = 'active' WHERE api_key = 'csk_lapsed'")
    assert _poll(monkeypatch) == ["csk_lapsed"]


def test_tradingview_relay_refuses_inactive_key(api, monkeypatch):
    _key("csk_canceled", "canceled")
    _watch("csk_canceled")

    async def deliver(*args):
        raise AssertionError("must not fire")

    monkeypatch.setattr(integrations, "_deliver", deliver)
    r = api.post("/integrations/tradingview/csk_canceled", content='{"symbol": "BTC"}')
    assert r.status_code == 403


def test_tradingview_relay_query_also_requires_active_key(api, monkeypatch):
    """The key goes inactive after the entry check, while sentiment is computed."""
    _watch()

    async def payload(symbol):
        with _db() as conn:
            conn.execute("UPDATE api_keys SET status = 'canceled' WHERE api_key = ?", (KEY,))
        return {"overall_sentiment": {"label": "bearish", "average_compound": -0.5}}

    async def deliver(*args):
        raise AssertionError("must not fire")

    monkeypatch.setattr(integrations, "compute_sentiment_payload", payload)
    monkeypatch.setattr(integrations, "_deliver", deliver)
    assert api.post(f"/integrations/tradingview/{KEY}", content='{"symbol": "BTC"}').status_code == 404


def test_tradingview_error_is_generic(api, monkeypatch):
    _watch()

    async def payload(symbol):
        return {"overall_sentiment": {"label": "bearish", "average_compound": -0.5}}

    async def post(url, body):
        return False, "delivery failed: HTTP 404"

    monkeypatch.setattr(integrations, "compute_sentiment_payload", payload)
    monkeypatch.setattr(alerts, "post_json", post)
    r = api.post(f"/integrations/tradingview/{KEY}", content='{"symbol": "BTC"}')
    assert r.json()["relayed_to"][0]["error"] == "delivery failed: HTTP 404"
