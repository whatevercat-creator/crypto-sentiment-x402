"""
The hourly archive as a product: GET /archive (free), GET /history/{symbol}
(x402, X402_HISTORY_PRICE_USD), hourly rows in /dataset/export, and the
home page mention. Also: a row written by the hourly logger has non-empty
drivers JSON, and /history returns it.

Run with: python -m pytest
"""

import asyncio
import base64
import csv
import io
import json
import os
import secrets
import tempfile
from datetime import datetime, timedelta, timezone

os.environ.setdefault("PAY_TO_ADDRESS", "0x000000000000000000000000000000000000dEaD")
os.environ.setdefault("CDP_API_KEY_ID", "test")
os.environ.setdefault("CDP_API_KEY_SECRET", "test")
os.environ.setdefault("BILLING_DB_PATH", os.path.join(tempfile.mkdtemp(), "billing.db"))

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from x402.extensions.bazaar import validate_discovery_extension, validate_discovery_extension_spec
from x402.schemas import SupportedKind, SupportedResponse

import app.billing as billing
from app import archive, hourly, main
from app import sentiment_service as svc
from app.billing import _db, _now_iso, init_db
from app.dataset import init_dataset_db
from app.sources.news import Headline

UTC = timezone.utc
DRIVERS = [{"title": "Bitcoin rallies", "source": "CoinDesk", "link": "https://x.test/a",
            "published": "2026-10-03T00:00:00Z", "score": 0.5}]


@pytest.fixture
def db(monkeypatch, tmp_path):
    monkeypatch.setattr(billing, "DB_PATH", str(tmp_path / "archive.db"))
    init_db()
    init_dataset_db()
    hourly.init_hourly_db()
    monkeypatch.setattr(archive, "HOURLY_SYMBOLS", ["BTC", "ETH"])


@pytest.fixture
def client(db, monkeypatch):
    monkeypatch.setattr(
        main.facilitator,
        "get_supported",
        lambda: SupportedResponse(kinds=[SupportedKind(x402Version=2, scheme="exact", network=main.CAIP2_NETWORK)]),
    )
    return TestClient(main.app)


def _insert(symbol, observed_at, compound=0.1, matcher=None, drivers=None):
    with _db() as conn:
        conn.execute(
            "INSERT INTO sentiment_hourly (symbol, observed_at, average_compound, sample_size, "
            "fear_greed_value, matcher, drivers) VALUES (?, ?, ?, 40, 50, ?, ?)",
            (symbol, observed_at.isoformat(), compound, matcher,
             None if drivers is None else json.dumps(drivers)),
        )


def _seed(now):
    """BTC every hour for 6h except 2 missing; ETH stopped after 2 readings."""
    base = (now - timedelta(hours=5)).replace(minute=0, second=2, microsecond=0)
    for i in (0, 1, 3, 5):
        whole = i >= 3
        _insert("BTC", base + timedelta(hours=i), matcher="whole_word" if whole else None,
                drivers=DRIVERS if whole else None)
    _insert("XRP", base, matcher="whole_word", drivers=[])
    _insert("XRP", base + timedelta(hours=1), matcher="whole_word", drivers=DRIVERS)
    return base


# --------------------------------------------------------------------------
# GET /archive
# --------------------------------------------------------------------------


def test_archive_summary_counts_and_missing_hours(db):
    now = datetime(2026, 10, 3, 12, 30, tzinfo=UTC)
    base = _seed(now)
    summary = archive.archive_summary(now)

    btc = summary["symbols"]["BTC"]
    assert btc["logging_now"] is True
    assert btc["first_reading"].startswith(base.strftime("%Y-%m-%dT%H:00:02"))
    assert (btc["rows"], btc["whole_word_rows"], btc["rows_with_drivers"]) == (4, 2, 2)
    # Hours 2 and 4 are missing; hour 5 (12:00) is the current hour and was logged.
    assert btc["missing_hours"] == 2
    assert [g["hours"] for g in btc["missing"]] == [1, 1]

    xrp = summary["symbols"]["XRP"]  # not in HOURLY_SYMBOLS: no gap after its last reading
    assert xrp["logging_now"] is False and xrp["missing_hours"] == 0
    assert xrp["rows_with_drivers"] == 1  # "[]" is not drivers data
    assert summary["total_rows"] == 6
    assert "never backfilled" in summary["recording"]
    assert summary["get_readings"]["price_usd"] == archive.HISTORY_PRICE_USD


def test_current_hour_not_missing_during_grace_but_missing_after(db):
    _insert("BTC", datetime(2026, 10, 3, 10, 0, 2, tzinfo=UTC))
    _insert("BTC", datetime(2026, 10, 3, 11, 0, 2, tzinfo=UTC))
    early = archive.archive_summary(datetime(2026, 10, 3, 12, 5, tzinfo=UTC))
    late = archive.archive_summary(datetime(2026, 10, 3, 12, 30, tzinfo=UTC))
    assert early["symbols"]["BTC"]["missing_hours"] == 0
    assert late["symbols"]["BTC"]["missing"] == [
        {"from": "2026-10-03T12:00:00Z", "to": "2026-10-03T12:00:00Z", "hours": 1}
    ]


def test_archive_is_free_json_and_html(client):
    _seed(datetime.now(UTC))
    r = client.get("/archive")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/json")
    assert set(r.json()["symbols"]) == {"BTC", "XRP"}

    html = client.get("/archive", headers={"Accept": "text/html"})
    assert html.status_code == 200 and html.headers["content-type"].startswith("text/html")
    assert "never backfilled" in html.text and "BTC" in html.text and "<script" not in html.text


def test_archive_empty(client):
    body = client.get("/archive").json()
    assert body["symbols"] == {} and body["total_rows"] == 0
    assert "No readings recorded yet" in client.get("/archive", headers={"Accept": "text/html"}).text


# --------------------------------------------------------------------------
# GET /history/{symbol}: paywall and Bazaar
# --------------------------------------------------------------------------


def _assert_valid(ext):
    for check in (validate_discovery_extension_spec, validate_discovery_extension):
        result = check(ext)
        assert result.valid, result.errors


def test_history_price_defaults_to_five_cents():
    if "X402_HISTORY_PRICE_USD" not in os.environ:
        assert archive.HISTORY_PRICE_USD == "$0.05"
    assert main.routes["GET /history/:symbol"].accepts[0].price == archive.HISTORY_PRICE_USD


def test_history_bazaar_declaration_validates():
    route = main.routes["GET /history/:symbol"]
    _assert_valid(route.extensions["bazaar"])
    assert route.extensions["bazaar"]["info"]["output"]["example"] == archive.HISTORY_EXAMPLE
    assert len(route.description) <= 500


def test_unpaid_history_gets_402_with_its_own_price_and_extension(client):
    r = client.get("/history/BTC?start=2026-10-01T00:00:00Z")
    assert r.status_code == 402
    challenge = json.loads(base64.b64decode(r.headers["payment-required"]))
    assert r.json() == challenge
    [accept] = challenge["accepts"]
    usdc_units = round(float(archive.HISTORY_PRICE_USD.lstrip("$")) * 1_000_000)
    assert accept["amount"] == str(usdc_units)
    ext = challenge["extensions"]["bazaar"]
    _assert_valid(ext)
    assert ext["routeTemplate"] == "/history/:symbol"
    assert ext["info"]["input"]["pathParams"] == {"symbol": "BTC"}


def test_openapi_and_llms_list_history_and_archive(client):
    spec = client.get("/openapi.json").json()
    op = spec["paths"]["/history/{symbol}"]["get"]
    assert op["x-payment-info"]["price"]["amount"] == archive.HISTORY_PRICE_USD.lstrip("$")
    assert spec["paths"]["/archive"]["get"]["security"] == []
    assert "/history/{symbol}" in spec["info"]["x-guidance"]
    llms = client.get("/llms.txt").text
    assert "/history/{symbol}" in llms and "GET /archive" in llms


# --------------------------------------------------------------------------
# GET /history/{symbol}: the handler (payment is the middleware's job)
# --------------------------------------------------------------------------


def _call(symbol, start=None, end=None):
    return json.loads(asyncio.run(main.get_history(symbol, start=start, end=end)).body)


def test_history_defaults_to_last_seven_days_with_drivers_or_null(db):
    now = datetime.now(UTC)
    _insert("BTC", now - timedelta(days=8))  # outside default range
    _insert("BTC", now - timedelta(days=2), matcher=None, drivers=None)  # pre-drivers row
    _insert("BTC", now - timedelta(hours=1), matcher="whole_word", drivers=DRIVERS)
    body = _call("btc")
    assert body["symbol"] == "BTC" and body["count"] == 2
    assert [r["drivers"] for r in body["rows"]] == [None, DRIVERS]
    assert [r["matcher"] for r in body["rows"]] == [None, "whole_word"]
    start, end = archive._parse_ts(body["start"]), archive._parse_ts(body["end"])
    assert timedelta(days=7) - timedelta(seconds=5) < end - start <= timedelta(days=7)
    assert "never backfilled" in body["recording"]


def test_history_explicit_range_is_end_exclusive(db):
    for h in range(5):
        _insert("ETH", datetime(2026, 10, 3, h, 0, 2, tzinfo=UTC), compound=h / 10)
    body = _call("ETH", "2026-10-03T01:00:00Z", "2026-10-03T03:00:02Z")
    assert [r["average_compound"] for r in body["rows"]] == [0.1, 0.2]


@pytest.mark.parametrize(
    "start,end,message",
    [
        ("2026-09-01T00:00:00Z", "2026-10-03T00:00:00Z", "at most 30 days"),
        ("2026-10-03T00:00:00Z", "2026-10-02T00:00:00Z", "before end"),
        ("yesterday", None, "ISO 8601"),
    ],
)
def test_history_rejects_bad_ranges_with_400(db, start, end, message):
    _insert("BTC", datetime.now(UTC))
    with pytest.raises(HTTPException) as e:
        _call("BTC", start, end)
    assert e.value.status_code == 400 and message in e.value.detail


def test_history_exactly_30_days_allowed(db):
    _insert("BTC", datetime(2026, 9, 10, tzinfo=UTC))
    assert _call("BTC", "2026-09-03T00:00:00Z", "2026-10-03T00:00:00Z")["count"] == 1


def test_history_unknown_or_invalid_symbol(db):
    with pytest.raises(HTTPException) as e:
        _call("DOGE")
    assert e.value.status_code == 404 and "/archive" in e.value.detail
    with pytest.raises(HTTPException) as e:
        _call("BT-C")
    assert e.value.status_code == 400


def test_row_written_by_hourly_logger_has_nonempty_drivers(db, monkeypatch):
    """The drivers column was added 2026-10-03 (35a2474); a reading the
    logger actually writes must carry real drivers JSON, and /history must
    return it."""
    headlines = [
        Headline(title=t, source="CoinDesk", link=f"https://x.test/{i}", published="2026-10-03T00:00:00Z", text=t)
        for i, t in enumerate(["Bitcoin rally is great", "Bitcoin exchange hacked", "Bitcoin ETF approved, good news"])
    ]

    async def fake_items(limit_per_feed=30):
        return headlines

    async def fake_fng():
        return {"value": 50, "classification": "Neutral"}

    monkeypatch.setattr(svc, "fetch_feed_items", fake_items)
    monkeypatch.setattr(svc, "fetch_fear_greed", fake_fng)
    asyncio.run(hourly.log_hour(["BTC"]))

    with _db() as conn:
        raw = conn.execute("SELECT drivers FROM sentiment_hourly WHERE symbol = 'BTC'").fetchone()[0]
    stored = json.loads(raw)
    assert isinstance(stored, list) and 1 <= len(stored) <= 3
    assert all(d["title"] and d["link"] and isinstance(d["score"], float) for d in stored)

    [row] = _call("BTC")["rows"]
    assert row["drivers"] == stored and row["matcher"] == "whole_word"


# --------------------------------------------------------------------------
# /dataset/export and the home page
# --------------------------------------------------------------------------


def _key(tier):
    key = "csk_test_" + secrets.token_hex(8)
    now = _now_iso()
    with _db() as conn:
        conn.execute(
            "INSERT INTO api_keys (api_key, email, tier, status, period_calls_used, period_start, created_at) "
            "VALUES (?, ?, ?, 'active', 0, ?, ?)",
            (key, f"{tier}@example.com", tier, now, now),
        )
    return key


def test_dataset_export_includes_hourly_rows(client):
    _seed(datetime(2026, 10, 3, 12, 30, tzinfo=UTC))
    key = _key("pro")
    body = client.get("/dataset/export?format=json", headers={"X-API-Key": key}).json()
    assert body["hourly_count"] == 6 and "rows" in body
    assert {r["symbol"] for r in body["hourly_rows"]} == {"BTC", "XRP"}
    assert any(r["drivers"] == DRIVERS for r in body["hourly_rows"])

    only = client.get("/dataset/export?format=json&symbol=xrp&since=2026-10-01", headers={"X-API-Key": key}).json()
    assert only["hourly_count"] == 2 and all(r["symbol"] == "XRP" for r in only["hourly_rows"])

    r = client.get("/dataset/export?table=hourly", headers={"X-API-Key": key})
    assert r.status_code == 200 and "hourly" in r.headers["content-disposition"]
    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert len(rows) == 6 and rows[0].keys() >= {"symbol", "observed_at", "matcher", "drivers"}
    assert any(json.loads(x["drivers"]) == DRIVERS for x in rows if x["drivers"])

    assert client.get("/dataset/export?format=json", headers={"X-API-Key": _key("starter")}).status_code == 403
    assert "hourly" in client.get("/dataset/info").json()


def test_home_page_mentions_archive(client):
    html = client.get("/", headers={"Accept": "text/html"}).text
    assert 'href="/archive"' in html and "/history/{symbol}" in html
    assert archive.HISTORY_PRICE_USD in html and "never backfilled" in html
