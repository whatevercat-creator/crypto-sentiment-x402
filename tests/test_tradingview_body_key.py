"""
TradingView relay with the API key in the alert message (POST
/integrations/tradingview): the key never reaches the relayed webhook,
Discord or Telegram message. The deprecated path form keeps working, says
it's deprecated, logs a key-free tradingview_path_key line, and the
uvicorn access log redacts its key.
"""

import json
import logging

import pytest

from tests.test_alert_watches import KEY, _key, api  # noqa: F401  (shared fixture)
from app import alerts, integrations, main
from app.billing import _db, _now_iso

SECRET = "csk_tvSecretValue0123456789abcdefXYZ"  # long, so a leak is unmistakable
TARGETS = {
    "webhook": "https://hook.customer.dev/tv",
    "discord": "https://discord.com/api/webhooks/123/abc",
    "telegram": "987654321",
}


def _watch_on(key, channel_type):
    with _db() as conn:
        conn.execute(
            "INSERT INTO watches (api_key, symbol, channel_type, channel_target, threshold, created_at) "
            "VALUES (?, 'BTC', ?, ?, 0.3, ?)",
            (key, channel_type, TARGETS[channel_type], _now_iso()),
        )


@pytest.fixture
def relay(api, monkeypatch):  # noqa: F811
    """A key with one watch per channel, sentiment stubbed, and every
    outgoing POST captured instead of sent."""
    _key(SECRET)
    for channel in TARGETS:
        _watch_on(SECRET, channel)

    async def payload(symbol):
        return {"overall_sentiment": {"label": "bullish", "average_compound": 0.42}}

    sent = []

    async def post(url, body):
        sent.append((str(url), body))
        return True, ""

    monkeypatch.setattr(integrations, "compute_sentiment_payload", payload)
    monkeypatch.setattr(alerts, "post_json", post)
    monkeypatch.setattr(alerts, "TELEGRAM_BOT_TOKEN", "bot-token-for-tests")
    return api, sent


def _body(**fields):
    return json.dumps({"api_key": SECRET, "symbol": "BINANCE:BTCUSDT", "price": "97000", **fields})


# --- body form --------------------------------------------------------------

def test_body_key_relays_to_every_channel(relay):
    client, sent = relay
    r = client.post("/integrations/tradingview", content=_body())
    assert r.status_code == 200
    data = r.json()
    assert data["symbol"] == "BTC"
    assert {x["channel_type"] for x in data["relayed_to"]} == set(TARGETS)
    assert all(x["delivered"] for x in data["relayed_to"])
    assert "deprecated" not in data
    assert len(sent) == 3


def test_body_key_never_reaches_any_channel(relay):
    client, sent = relay
    client.post("/integrations/tradingview", content=_body())
    for url, body in sent:
        assert SECRET not in json.dumps(body), url
    webhook = next(body for url, body in sent if url.startswith("https://hook.customer.dev"))
    assert webhook["tradingview_alert"] == {"symbol": "BINANCE:BTCUSDT", "price": "97000"}
    assert "api_key" not in webhook["tradingview_alert"]


def test_body_key_not_in_response(relay):
    client, _ = relay
    assert SECRET not in client.post("/integrations/tradingview", content=_body()).text


@pytest.mark.parametrize(
    "content",
    [
        "",
        "BTCUSD",
        '{"symbol": "BTC"}',
        '{"api_key": "", "symbol": "BTC"}',
        '{"api_key": 123, "symbol": "BTC"}',
        '["csk_tvSecretValue0123456789abcdefXYZ"]',
    ],
    ids=["empty", "plain-text", "no-key", "blank-key", "non-string-key", "json-list"],
)
def test_body_form_without_a_key_is_401(relay, content):
    client, sent = relay
    r = client.post("/integrations/tradingview", content=content)
    assert r.status_code == 401
    assert "api_key" in r.json()["detail"]
    assert sent == []


def test_body_form_unknown_key_is_401(relay):
    client, sent = relay
    r = client.post("/integrations/tradingview", content=_body(api_key="csk_nope"))
    assert r.status_code == 401
    assert sent == []


def test_body_form_inactive_key_is_403(relay):
    client, sent = relay
    with _db() as conn:
        conn.execute("UPDATE api_keys SET status = 'canceled' WHERE api_key = ?", (SECRET,))
    assert client.post("/integrations/tradingview", content=_body()).status_code == 403
    assert sent == []


def test_body_form_without_symbol_is_400_and_never_echoes_the_key(relay):
    client, sent = relay
    r = client.post("/integrations/tradingview", content=json.dumps({"api_key": SECRET}))
    assert r.status_code == 400
    assert SECRET not in r.text
    assert sent == []


def test_body_form_logs_no_path_key_line(relay, caplog):
    client, _ = relay
    with caplog.at_level(logging.INFO):
        client.post("/integrations/tradingview", content=_body())
    assert "tradingview_path_key" not in caplog.text
    assert SECRET not in caplog.text


# --- deprecated path form ---------------------------------------------------

def test_path_form_still_relays_and_says_deprecated(relay):
    client, sent = relay
    r = client.post(f"/integrations/tradingview/{SECRET}", content='{"symbol": "BTCUSDT"}')
    assert r.status_code == 200
    data = r.json()
    assert data["symbol"] == "BTC"
    assert len(data["relayed_to"]) == 3
    assert "deprecated" in data and "/integrations/tradingview" in data["deprecated"]
    assert "60 days" in data["deprecated"]


def test_path_form_logs_one_key_free_line(relay, caplog):
    client, _ = relay
    with caplog.at_level(logging.INFO, logger="app.integrations"):
        client.post(f"/integrations/tradingview/{SECRET}", content='{"symbol": "BTC"}')
    lines = [json.loads(r.getMessage()) for r in caplog.records
             if r.name == "app.integrations" and "tradingview_path_key" in r.getMessage()]
    assert lines == [{"event": "tradingview_path_key", "key_prefix": SECRET[:8]}]
    assert SECRET not in caplog.text


def test_path_form_logs_even_for_a_rejected_key(relay, caplog):
    """The line counts every path-form call, so 60 quiet days means no one uses it."""
    client, _ = relay
    with caplog.at_level(logging.INFO, logger="app.integrations"):
        assert client.post("/integrations/tradingview/csk_unknownkey9", content='{"symbol": "BTC"}').status_code == 401
    assert '"key_prefix": "csk_unkn"' in caplog.text


def test_path_form_strips_a_key_in_the_message_too(relay):
    client, sent = relay
    client.post(f"/integrations/tradingview/{SECRET}", content=_body())
    for _, body in sent:
        assert SECRET not in json.dumps(body)


def test_path_form_plain_text_fallback_still_works(relay):
    client, sent = relay
    r = client.post(f"/integrations/tradingview/{SECRET}", content="BTCUSD")
    assert r.status_code == 200
    webhook = next(body for url, body in sent if url.startswith("https://hook.customer.dev"))
    assert webhook["tradingview_alert"] == "BTCUSD"


def test_path_form_json_without_symbol_is_400(relay):
    client, sent = relay
    r = client.post(f"/integrations/tradingview/{SECRET}", content='{"price": 1}')
    assert r.status_code == 400
    assert sent == []


# --- access-log redaction ---------------------------------------------------

@pytest.mark.parametrize(
    "path,expected",
    [
        (f"/integrations/tradingview/{SECRET}", "/integrations/tradingview/[redacted]"),
        (f"/integrations/tradingview/{SECRET}?x=1", "/integrations/tradingview/[redacted]?x=1"),
        (f"/integrations/tradingview/{SECRET}/extra", "/integrations/tradingview/[redacted]/extra"),
        (f"/prefix/integrations/tradingview/{SECRET}", "/prefix/integrations/tradingview/[redacted]"),
        ("/integrations/tradingview", "/integrations/tradingview"),
        ("/integrations/tradingview/", "/integrations/tradingview/"),
        ("/sentiment/BTC", "/sentiment/BTC"),
        ("/v1/sentiment/BTC?x=csk_abc", "/v1/sentiment/BTC?x=csk_abc"),
    ],
)
def test_redact_path(path, expected):
    assert integrations.redact_path(path) == expected


def _uvicorn_record(path):
    """A LogRecord shaped like uvicorn's access line."""
    return logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 0,
        '%s - "%s %s HTTP/%s" %d', ("203.0.113.7:0", "POST", path, "1.1", 200), None,
    )


def test_filter_redacts_the_uvicorn_line():
    record = _uvicorn_record(f"/integrations/tradingview/{SECRET}")
    assert integrations.AccessLogKeyRedactor().filter(record) is True
    assert record.getMessage() == '203.0.113.7:0 - "POST /integrations/tradingview/[redacted] HTTP/1.1" 200'


def test_filter_leaves_other_lines_alone():
    record = _uvicorn_record("/sentiment/BTC")
    integrations.AccessLogKeyRedactor().filter(record)
    assert record.getMessage() == '203.0.113.7:0 - "POST /sentiment/BTC HTTP/1.1" 200'


@pytest.mark.parametrize("args", [None, (), ("only-one",), {"a": 1}, (1, 2, 3)])
def test_filter_never_drops_or_raises_on_odd_records(args):
    record = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 0, "msg", None, None)
    record.args = args
    assert integrations.AccessLogKeyRedactor().filter(record) is True


def test_filter_also_covers_an_already_formatted_message():
    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 0,
        f'1.2.3.4:0 - "POST /integrations/tradingview/{SECRET} HTTP/1.1" 200', None, None,
    )
    integrations.AccessLogKeyRedactor().filter(record)
    assert SECRET not in record.getMessage()


def test_filter_is_installed_on_uvicorn_access(caplog):
    assert main  # importing app.main installs it
    access = logging.getLogger("uvicorn.access")
    assert any(isinstance(f, integrations.AccessLogKeyRedactor) for f in access.filters)
    with caplog.at_level(logging.INFO, logger="uvicorn.access"):
        access.info('%s - "%s %s HTTP/%s" %d', "1.2.3.4:0", "POST",
                    f"/integrations/tradingview/{SECRET}", "1.1", 200)
    assert SECRET not in caplog.text
    assert "/integrations/tradingview/[redacted]" in caplog.text
