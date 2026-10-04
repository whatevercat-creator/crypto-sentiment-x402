"""
The 2026-10-04 method change: 72-hour window, recency weighting (weight
halves every 24 hours), the "insufficient recent news" label and what reads
it (alerts, TradingView relay), DL News removed, feed health, the new
hourly columns, the matched-headlines table, leadlag options and the
methodology note.

Run with: python -m pytest
"""

import asyncio
import json
import logging
import os
import secrets
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone

os.environ.setdefault("PAY_TO_ADDRESS", "0x000000000000000000000000000000000000dEaD")
os.environ.setdefault("CDP_API_KEY_ID", "test")
os.environ.setdefault("CDP_API_KEY_SECRET", "test")
os.environ.setdefault("BILLING_DB_PATH", os.path.join(tempfile.mkdtemp(), "billing.db"))

import httpx
import pytest
from fastapi.testclient import TestClient
from x402.schemas import SupportedKind, SupportedResponse

import app.billing as billing
from app import alerts, archive, hourly, integrations, main, window
from app import sentiment_service as svc
from app.billing import _db, _now_iso, init_db
from app.dataset import init_dataset_db
from app.sentiment import score_text
from app.sources import news
from app.sources.news import Headline

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import leadlag  # noqa: E402

UTC = timezone.utc
NOW = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
SECRET = "SECRET-ARTICLE-BODY"


def _iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


def _h(title, hours_old, desc="", link=None, source="CoinDesk"):
    published = None if hours_old is None else _iso(NOW - timedelta(hours=hours_old))
    return Headline(title=title, source=source, link=link, published=published, text=f"{title} {desc}")


POS = "Bitcoin rally is great, bullish breakout"
NEG = "Bitcoin exchange hacked in massive scam"


# --------------------------------------------------------------------------
# Window and weighting
# --------------------------------------------------------------------------


def test_named_constants():
    assert (window.MAX_AGE_HOURS, window.HALF_LIFE_HOURS, window.MIN_EFFECTIVE_SAMPLE) == (72, 24, 5)
    assert window.WINDOW_VERSION == "72h-hl24"
    assert window.INSUFFICIENT_LABEL == "insufficient recent news"


def test_72_hour_cap_undated_and_future_dates():
    items = [
        _h(POS, 71.9),
        _h(POS, 72.1),            # too old
        _h(POS, None),            # undated: left out
        _h(POS, -0.5),            # 30 min "in the future": counted as age 0
        _h(POS, -3),              # 3 h in the future: bad date, left out
        Headline(POS, "X", None, "not a date", POS),
    ]
    scored = window.in_window(items, NOW)
    assert [round(s.age_hours, 1) for s in scored] == [71.9, 0.0]


def test_weight_halves_every_24_hours():
    assert window.weight_for(0) == 1
    assert window.weight_for(24) == pytest.approx(0.5)
    assert window.weight_for(48) == pytest.approx(0.25)
    assert window.weight_for(72) == pytest.approx(0.125)


def test_weighted_score_unweighted_score_and_effective_sample():
    items = [_h(POS, 0), _h(NEG, 48)] + [_h("Bitcoin conference opens", h) for h in (1, 2, 3, 4, 5)]
    overall = window.summarize(window.in_window(items, NOW))
    weights = [window.weight_for(a) for a in (0, 48, 1, 2, 3, 4, 5)]
    scores = [score_text(h.text) for h in items]
    weighted = sum(w * s for w, s in zip(weights, scores)) / sum(weights)
    assert overall["average_compound"] == round(weighted, 4)
    assert overall["unweighted_compound_72h"] == round(sum(scores) / len(scores), 4)
    assert overall["effective_sample_size"] == round(sum(weights) ** 2 / sum(w * w for w in weights), 2)
    assert overall["newest_headline_age_hours"] == 0.0
    assert overall["window"] == "72h-hl24" and overall["sample_size"] == 7
    # The fresh positive headline outweighs the 2-day-old negative one.
    assert overall["average_compound"] > overall["unweighted_compound_72h"]


def test_label_insufficient_below_effective_sample_of_five():
    four = [_h(POS, 1) for _ in range(4)]
    five = [_h(POS, 1) for _ in range(5)]
    assert window.summarize(window.in_window(four, NOW))["label"] == "insufficient recent news"
    assert window.summarize(window.in_window(five, NOW))["label"] == "bullish"
    # Eight headlines but mostly old: effective sample below 5.
    stale = [_h(POS, 1)] + [_h(POS, 70) for _ in range(7)]
    overall = window.summarize(window.in_window(stale, NOW))
    assert overall["sample_size"] == 8 and overall["effective_sample_size"] < 5
    assert overall["label"] == "insufficient recent news"
    empty = window.summarize([])
    assert empty["label"] == "insufficient recent news" and empty["newest_headline_age_hours"] is None


def test_payload_fields_and_no_article_text():
    payload = svc.build_payload("BTC", [_h(POS, 1, SECRET, "https://x.test/1")] * 6, None, NOW)
    overall = payload["overall_sentiment"]
    for key in ("unweighted_compound_72h", "effective_sample_size", "newest_headline_age_hours", "window"):
        assert key in overall
    assert payload["breakdown"]["news"] == overall
    assert SECRET not in json.dumps(payload)


# --------------------------------------------------------------------------
# Things that read the label
# --------------------------------------------------------------------------


@pytest.fixture
def db(monkeypatch, tmp_path):
    monkeypatch.setattr(billing, "DB_PATH", str(tmp_path / "w.db"))
    init_db()
    init_dataset_db()
    alerts.init_alerts_db()
    hourly.init_hourly_db()


def _watch(label, compound):
    with _db() as conn:
        conn.execute(
            "INSERT INTO watches (api_key, symbol, channel_type, channel_target, threshold, last_label, "
            "last_compound, created_at) VALUES ('k', 'BTC', 'webhook', 'https://hook.test', 0.3, ?, ?, ?)",
            (label, compound, _now_iso()),
        )


def _run_poll(monkeypatch, label, compound):
    sent = []

    async def payload(symbol):
        return {"overall_sentiment": {"label": label, "average_compound": compound}}

    async def deliver(watch, message, event):
        sent.append(event)
        return True, None

    monkeypatch.setattr(alerts, "compute_sentiment_payload", payload)
    monkeypatch.setattr(alerts, "_deliver", deliver)
    asyncio.run(alerts._poll_once())
    with _db() as conn:
        row = conn.execute("SELECT last_label, last_compound FROM watches").fetchone()
    return sent, tuple(row)


def test_insufficient_label_never_fires_an_alert_or_replaces_baseline(db, monkeypatch):
    _watch("bullish", 0.4)
    sent, baseline = _run_poll(monkeypatch, "insufficient recent news", 0.0)
    assert sent == [] and baseline == ("bullish", 0.4)
    # The next real reading compares with the last real one.
    sent, baseline = _run_poll(monkeypatch, "bearish", -0.3)
    assert [e["old_label"] for e in sent] == ["bullish"] and baseline == ("bearish", -0.3)


def test_insufficient_on_first_check_keeps_it_a_first_check(db, monkeypatch):
    _watch(None, None)
    sent, baseline = _run_poll(monkeypatch, "insufficient recent news", 0.0)
    assert sent == [] and baseline == (None, None)
    sent, baseline = _run_poll(monkeypatch, "neutral", 0.0)
    assert sent == [] and baseline == ("neutral", 0.0)  # baseline only, as before


def test_tradingview_relay_message_with_insufficient_label(db, monkeypatch):
    key = "csk_test_" + secrets.token_hex(8)
    with _db() as conn:
        conn.execute(
            "INSERT INTO api_keys (api_key, email, tier, status, period_calls_used, period_start, created_at) "
            "VALUES (?, 'a@example.com', 'pro', 'active', 0, ?, ?)",
            (key, _now_iso(), _now_iso()),
        )
        conn.execute(
            "INSERT INTO watches (api_key, symbol, channel_type, channel_target, created_at) "
            "VALUES (?, 'BTC', 'webhook', 'https://hook.test', ?)",
            (key, _now_iso()),
        )
    messages = []

    async def payload(symbol):
        return {"overall_sentiment": {"label": "insufficient recent news", "average_compound": 0.0}}

    async def deliver(watch, message, event):
        messages.append(message)
        return True, None

    monkeypatch.setattr(integrations, "compute_sentiment_payload", payload)
    monkeypatch.setattr(integrations, "_deliver", deliver)
    r = TestClient(main.app).post(f"/integrations/tradingview/{key}", content='{"symbol": "BTCUSDT"}')
    assert r.status_code == 200 and r.json()["sentiment_label"] == "insufficient recent news"
    assert messages[0].endswith("current sentiment: insufficient recent news")


# --------------------------------------------------------------------------
# DL News removed, everywhere
# --------------------------------------------------------------------------


@pytest.fixture
def client(db, monkeypatch):
    monkeypatch.setattr(
        main.facilitator,
        "get_supported",
        lambda: SupportedResponse(kinds=[SupportedKind(x402Version=2, scheme="exact", network=main.CAIP2_NETWORK)]),
    )
    return TestClient(main.app)


def test_dl_news_gone_and_outlet_count_is_nine(client):
    assert "DL News" not in news.NEWS_OUTLETS and len(news.NEWS_OUTLETS) == 9
    assert not any("dlnews" in s for s in svc.SOURCES)
    texts = {
        "route": main.routes["GET /sentiment/:symbol"].description,
        "llms": client.get("/llms.txt").text,
        "home": client.get("/", headers={"Accept": "text/html"}).text,
        "openapi": json.dumps(client.get("/openapi.json").json()),
        "transparency_sources": json.dumps(client.get("/transparency").json()["data_sources"]),
    }
    for name, text in texts.items():
        assert "DL News" not in text and "dlnews" not in text, name
        assert "10 crypto" not in text and "10 news" not in text, name
    assert "9 crypto news RSS outlets" in texts["route"] and len(texts["route"]) <= 500
    assert "9 crypto news RSS outlets" in texts["llms"]
    assert "insufficient recent news" in texts["llms"] and "72 hours" in texts["llms"]
    assert "insufficient recent news" in texts["home"]
    assert "dlnews.com RSS" in client.get("/transparency").json()["sources_removed"]


def test_bazaar_example_validates_with_window_fields():
    import jsonschema

    ext = main.routes["GET /sentiment/:symbol"].extensions["bazaar"]
    example_schema = ext["schema"]["properties"]["output"]["properties"]["example"]
    jsonschema.validate(ext["info"]["output"]["example"], example_schema)
    assert ext["info"]["output"]["example"]["overall_sentiment"]["window"] == "72h-hl24"


# --------------------------------------------------------------------------
# Feed health
# --------------------------------------------------------------------------


def _items(hours_old):
    return [_h(POS, h) for h in hours_old]


def test_feed_health_flags_stale_and_warns_once_per_hour(monkeypatch, caplog):
    monkeypatch.setattr(news, "FEED_HEALTH", {})
    monkeypatch.setattr(news, "_last_warned", {})
    with caplog.at_level(logging.WARNING, logger="app.sources.news"):
        news._record_health("CoinDesk", NOW, _items([1, 5]))
        news._record_health("NewsBTC", NOW, _items([50, 60]))
        news._record_health("NewsBTC", NOW + timedelta(minutes=30), _items([50.5]))
        news._record_health("NewsBTC", NOW + timedelta(minutes=61), _items([51]))
        news._record_health("Decrypt", NOW, error="ConnectError")
    assert news.FEED_HEALTH["CoinDesk"]["status"] == "ok" and not news.FEED_HEALTH["CoinDesk"]["stale"]
    assert news.FEED_HEALTH["NewsBTC"]["stale"] and news.FEED_HEALTH["NewsBTC"]["status"] == "stale"
    assert news.FEED_HEALTH["Decrypt"]["status"] == "error" and not news.FEED_HEALTH["Decrypt"]["stale"]
    warnings = [r for r in caplog.records if "NewsBTC looks stale" in r.getMessage()]
    assert len(warnings) == 2  # at :00 and again after an hour, not at :30
    assert news.feed_health()["stale_outlets"] == ["NewsBTC"]


def test_stale_outlet_not_dropped(monkeypatch):
    old = (datetime.now(UTC) - timedelta(days=5)).strftime("%a, %d %b %Y %H:%M:%S +0000")
    rss = f"<rss><channel><item><title>Bitcoin dips</title><link>https://x.test/o</link><pubDate>{old}</pubDate></item></channel></rss>"
    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        return real(*args, transport=httpx.MockTransport(lambda r: httpx.Response(200, content=rss.encode())), **kwargs)

    monkeypatch.setattr(news.httpx, "AsyncClient", factory)
    monkeypatch.setattr(news, "RSS_FEEDS", [news.NEWS_OUTLETS["NewsBTC"]])
    monkeypatch.setattr(news, "FEED_HEALTH", {})
    items = asyncio.run(news.fetch_feed_items())
    assert len(items) == 1  # still fetched and returned...
    assert news.FEED_HEALTH["NewsBTC"]["stale"]
    assert window.in_window(items) == []  # ...the window keeps it out of the score


def test_transparency_shows_feed_health(client, monkeypatch):
    monkeypatch.setattr(news, "FEED_HEALTH", {})
    sixty_hours_ago = _iso(datetime.now(UTC) - timedelta(hours=60))
    news._record_health("NewsBTC", datetime.now(UTC), [Headline("t", "NewsBTC", None, sixty_hours_ago, "t")])
    health = client.get("/transparency").json()["feed_health"]
    assert health["stale_outlets"] == ["NewsBTC"] and health["stale_after_hours"] == 48
    assert set(health["outlets"]) == set(news.NEWS_OUTLETS)
    assert "72h-hl24" in client.get("/transparency").json()["scoring_window"]


# --------------------------------------------------------------------------
# Hourly rows and the matched-headlines table
# --------------------------------------------------------------------------


def _fake_inputs(monkeypatch, items):
    async def fake_items(limit_per_feed=30):
        return items

    async def fake_fng():
        return {"value": 50, "classification": "Neutral"}

    monkeypatch.setattr(svc, "fetch_feed_items", fake_items)
    monkeypatch.setattr(svc, "fetch_fear_greed", fake_fng)


def _recent(title, hours, link=None, desc=SECRET):
    published = _iso(datetime.now(UTC) - timedelta(hours=hours))
    return Headline(title=title, source="CoinDesk", link=link, published=published, text=f"{title} {desc}")


def test_hourly_row_stores_window_columns_and_old_rows_stay_null(db, monkeypatch):
    with _db() as conn:
        conn.execute("INSERT INTO sentiment_hourly (symbol, observed_at, average_compound) VALUES ('BTC', '2026-10-01T00:00:01+00:00', 0.1)")
    items = [_recent(POS, h, f"https://x.test/{h}") for h in range(1, 8)]
    _fake_inputs(monkeypatch, items)
    asyncio.run(hourly.log_hour(["BTC"]))
    with _db() as conn:
        rows = conn.execute(
            'SELECT average_compound, unweighted_compound_72h, effective_sample_size, '
            'newest_headline_age_hours, "window" FROM sentiment_hourly ORDER BY id'
        ).fetchall()
    assert tuple(rows[0])[1:] == (None, None, None, None)
    compound, unweighted, effective, newest, win = rows[1]
    assert win == "72h-hl24" and 5 < effective < 7 and 0.9 < newest < 1.1
    assert compound == unweighted  # identical headlines: weighting can't change the average


def test_matched_headlines_table(db, monkeypatch):
    items = [
        _recent("Bitcoin rally", 1, "https://x.test/a"),
        _recent("Bitcoin rally", 1, "https://x.test/a"),       # duplicate link
        _recent("Ethereum upgrade lands", 2, "https://x.test/b"),
        _recent("Weather report", 2, "https://x.test/c"),      # matches no coin
        _recent("Solana outage", 3, None),                     # no link: skipped
        Headline("Old XRP story", "CoinDesk", "https://x.test/d", _iso(datetime.now(UTC) - timedelta(days=90)), "Old XRP story"),
    ]
    first = _now_iso()
    assert hourly.store_headlines(items, first) == 3
    with _db() as conn:
        rows = {r[0]: r for r in conn.execute("SELECT link, title, source, published, first_seen, score, symbols FROM sentiment_headlines")}
    assert set(rows) == {"https://x.test/a", "https://x.test/b", "https://x.test/d"}
    assert json.loads(rows["https://x.test/a"][6]) == ["BTC"]
    assert rows["https://x.test/d"][3] is not None  # stored even though outside the window
    assert all(SECRET not in json.dumps(list(r)) for r in rows.values())
    assert rows["https://x.test/a"][5] == pytest.approx(score_text(f"Bitcoin rally {SECRET}"))

    # Seen again later, matching another coin too: first_seen kept, symbols merged.
    again = [Headline("Bitcoin rally", "CoinDesk", "https://x.test/a", items[0].published, "Bitcoin rally lifts Ether")]
    assert hourly.store_headlines(again, "2026-10-05T00:00:00+00:00") == 0
    with _db() as conn:
        first_seen, symbols = conn.execute("SELECT first_seen, symbols FROM sentiment_headlines WHERE link = 'https://x.test/a'").fetchone()
    assert first_seen == first and json.loads(symbols) == ["BTC", "ETH"]


def test_hourly_logger_writes_headlines(db, monkeypatch):
    _fake_inputs(monkeypatch, [_recent(POS, h, f"https://x.test/{h}") for h in range(1, 6)])
    asyncio.run(hourly.log_hour(["BTC"]))
    with _db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM sentiment_headlines").fetchone()[0] == 5


def test_history_and_archive_carry_window_fields(db, monkeypatch):
    monkeypatch.setattr(archive, "HOURLY_SYMBOLS", ["BTC"])
    now = datetime.now(UTC)
    with _db() as conn:
        conn.execute("INSERT INTO sentiment_hourly (symbol, observed_at, average_compound) VALUES ('BTC', ?, 0.1)",
                     ((now - timedelta(hours=2)).isoformat(),))
    _fake_inputs(monkeypatch, [_recent(POS, h, f"https://x.test/{h}") for h in range(1, 7)])
    asyncio.run(hourly.log_hour(["BTC"]))
    body = json.loads(asyncio.run(main.get_history("BTC", start=None, end=None)).body)
    old, new = body["rows"]
    assert old["window"] is None and old["effective_sample_size"] is None
    assert new["window"] == "72h-hl24" and new["unweighted_compound_72h"] is not None
    assert "72h-hl24" in body["average_compound_note"]
    summary = archive.archive_summary()
    assert summary["symbols"]["BTC"]["window_rows"] == 1 and "window" in summary["average_compound_note"]


# --------------------------------------------------------------------------
# leadlag and validation.json
# --------------------------------------------------------------------------


def test_leadlag_window_only_and_score(tmp_path):
    path = tmp_path / "ll.db"
    conn = sqlite3.connect(path)
    conn.execute(
        'CREATE TABLE sentiment_hourly (symbol TEXT, observed_at TEXT, average_compound REAL, matcher TEXT, '
        'unweighted_compound_72h REAL, "window" TEXT)'
    )
    conn.executemany(
        'INSERT INTO sentiment_hourly VALUES (?, ?, ?, ?, ?, ?)',
        [
            ("BTC", "2026-10-03T22:00:02+00:00", 0.1, "whole_word", None, None),
            ("BTC", "2026-10-04T13:00:02+00:00", 0.2, "whole_word", 0.25, "72h-hl24"),
            ("BTC", "2026-10-04T14:00:02+00:00", 0.3, "whole_word", 0.35, "72h-hl24"),
        ],
    )
    conn.commit()
    conn.close()
    assert len(leadlag.load_sentiment(str(path), "BTC")) == 3
    assert sorted(leadlag.load_sentiment(str(path), "BTC", window_only=True).values()) == [0.2, 0.3]
    assert sorted(leadlag.load_sentiment(str(path), "BTC", window_only=True, score="unweighted").values()) == [0.25, 0.35]
    # Unweighted exists only on window rows anyway.
    assert len(leadlag.load_sentiment(str(path), "BTC", score="unweighted")) == 2


def test_leadlag_window_only_on_older_db(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE sentiment_hourly (symbol TEXT, observed_at TEXT, average_compound REAL)")
    conn.execute("INSERT INTO sentiment_hourly VALUES ('BTC', '2026-10-01T00:00:01+00:00', 0.1)")
    conn.commit()
    conn.close()
    assert leadlag.load_sentiment(str(path), "BTC", window_only=True) == {}
    assert leadlag.load_sentiment(str(path), "BTC", score="unweighted") == {}


def test_validation_note_and_expected_date(client):
    doc = main._load_validation()
    note = doc["methodology_changes"][-1]
    assert note["date"] == "2026-10-04" and "72 hours" in note["change"] and "DL News" in note["change"]
    assert "--window-only" in doc["method"]
    for symbol in ("BTC", "ETH"):
        entry = doc["symbols"][symbol]
        assert entry["first_results_expected"] == "2026-10-17" and "13 days" in entry["note"]
    html = client.get("/", headers={"Accept": "text/html"}).text
    assert "13 days" in html
    assert client.get("/transparency").json()["methodology_changes"][-1]["date"] == "2026-10-04"


def test_history_notes_do_not_share_names_with_row_fields(db):
    example = archive.HISTORY_EXAMPLE
    row_fields = set().union(*(row.keys() for row in example["rows"]))
    payload = archive.history_payload("BTC", datetime.now(UTC) - timedelta(days=1), datetime.now(UTC))
    for body in (example, payload, main.HISTORY_DISCOVERY["bazaar"]["info"]["output"]["example"]):
        assert not set(body) & row_fields
        assert "72h-hl24" in body["average_compound_note"] and "average_compound" not in body
    schema = main.HISTORY_DISCOVERY["bazaar"]["schema"]
    assert "average_compound_note" in json.dumps(schema)
    assert not set(archive.archive_summary()) & row_fields
    openapi = main.app.openapi()["paths"]["/history/{symbol}"]["get"]["responses"]["200"]
    assert "average_compound_note" in openapi["content"]["application/json"]["example"]
