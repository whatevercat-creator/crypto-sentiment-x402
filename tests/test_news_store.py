"""
Method 72h-hl24-v3 (2026-11-27): the rolling 72-hour story store
(app/news_store.py), and POL as a match term and input symbol for MATIC.

Run with: python -m pytest tests/test_news_store.py
"""

import asyncio
import json
import logging
import os
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone

os.environ.setdefault("PAY_TO_ADDRESS", "0x000000000000000000000000000000000000dEaD")
os.environ.setdefault("CDP_API_KEY_ID", "test")
os.environ.setdefault("CDP_API_KEY_SECRET", "test")
os.environ.setdefault("BILLING_DB_PATH", os.path.join(tempfile.mkdtemp(), "billing.db"))

import pytest

import app.billing as billing
from app import hourly, integrations, news_store, window
from app import sentiment_service as svc
from app.billing import _db, init_db
from app.coins import COIN_NAMES, mentions, validate_symbol
from app.sentiment import score_text
from app.sources.news import Headline, select_headlines

UTC = timezone.utc
NOW = datetime(2026, 11, 27, 12, 0, tzinfo=UTC)
SECRET = "SECRET-ARTICLE-BODY"


def _iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


def _h(title, hours_old, link="auto", source="CoinDesk", desc=SECRET, now=NOW):
    if link == "auto":
        link = f"https://x.test/{abs(hash((title, source))) % 10**8}"
    published = None if hours_old is None else _iso(now - timedelta(hours=hours_old))
    return Headline(title=title, source=source, link=link, published=published, text=f"{title} {desc}")


@pytest.fixture
def store(monkeypatch, tmp_path):
    monkeypatch.setattr(billing, "DB_PATH", str(tmp_path / "s.db"))
    monkeypatch.setattr(news_store, "ENABLED", True)
    init_db()
    hourly.init_hourly_db()


def _rows():
    with _db() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM news_items ORDER BY id")]


# --------------------------------------------------------------------------
# Insert and what a row holds
# --------------------------------------------------------------------------


def test_table_is_created_with_only_the_allowed_columns(store):
    with _db() as conn:
        columns = [r[1] for r in conn.execute("PRAGMA table_info(news_items)")]
    assert columns == ["id", "outlet", "title", "link", "published", "first_seen", "score", "coins"]


def test_insert_stores_score_and_coins_but_no_article_text(store):
    item = _h("Bitcoin rally lifts Ether", 2, link="https://x.test/a")
    news_store.with_store([item], NOW)
    [row] = _rows()
    assert row["outlet"] == "CoinDesk" and row["title"] == "Bitcoin rally lifts Ether"
    assert row["link"] == "https://x.test/a" and row["published"] == _iso(NOW - timedelta(hours=2))
    assert row["first_seen"] == NOW.isoformat(timespec="seconds")
    # Scored exactly as window.in_window scores a live item.
    assert row["score"] == pytest.approx(score_text(item.text[: window.SCORED_CHARS]))
    assert json.loads(row["coins"]) == ["BTC", "ETH"]
    assert SECRET not in json.dumps(row)


def test_items_without_a_parseable_date_are_not_stored(store):
    bad_date = Headline("Bitcoin bad date", "CoinDesk", "https://x.test/bad", "not a date", "Bitcoin bad date")
    out = news_store.with_store([_h("Bitcoin undated", None), bad_date], NOW)
    assert _rows() == []
    assert len(out) == 2  # still passed through; the window leaves them out
    assert window.in_window(out, NOW) == []


def test_items_already_past_the_store_cutoff_are_not_stored(store):
    news_store.with_store([_h("Bitcoin old", 80), _h("Bitcoin fresh", 1)], NOW)
    assert [r["title"] for r in _rows()] == ["Bitcoin fresh"]


def test_items_matching_no_coin_are_stored_with_empty_coins(store):
    news_store.with_store([_h("Weather report", 1)], NOW)
    [row] = _rows()
    assert json.loads(row["coins"]) == []


# --------------------------------------------------------------------------
# Dedupe
# --------------------------------------------------------------------------


def test_dedupe_by_link_keeps_first_seen_and_first_score(store):
    first = _h("Bitcoin rally is great", 2, link="https://x.test/a")
    news_store.with_store([first, first], NOW)
    later = NOW + timedelta(hours=1)
    # Same link, description edited by the feed: score stays as first seen,
    # coins are re-matched from the live text.
    edited = Headline(first.title, "CoinDesk", "https://x.test/a", first.published, "Bitcoin crash hack scam, Ether too")
    out = news_store.with_store([edited], later)
    [row] = _rows()
    assert row["first_seen"] == NOW.isoformat(timespec="seconds")
    assert row["score"] == pytest.approx(score_text(first.text[: window.SCORED_CHARS]))
    assert json.loads(row["coins"]) == ["BTC", "ETH"]
    assert len(out) == 1 and out[0].score == pytest.approx(row["score"]) and out[0].coins == ("BTC", "ETH")


def test_dedupe_by_outlet_and_title_without_a_link(store):
    a = _h("Bitcoin rally", 1, link=None, source="CoinDesk")
    b = _h("Bitcoin rally", 1, link=None, source="Decrypt")  # same title, other outlet: a different item
    news_store.with_store([a, a, b], NOW)
    news_store.with_store([a], NOW + timedelta(hours=1))
    assert sorted(r["outlet"] for r in _rows()) == ["CoinDesk", "Decrypt"]


def test_live_and_stored_are_merged_without_double_counting(store):
    old = [_h(f"Bitcoin story {i}", 30 + i) for i in range(4)]
    news_store.with_store(old, NOW)  # an earlier fetch
    live = [_h("Bitcoin story new", 1), old[0]]  # old[0] still in the feed
    out = news_store.with_store(live, NOW)
    titles = [h.title for h in out]
    assert titles == ["Bitcoin story new", "Bitcoin story 0", "Bitcoin story 1", "Bitcoin story 2", "Bitcoin story 3"]
    assert len(set(titles)) == len(titles)
    # Live items keep their text (sentiment_headlines still sees it); stored-only ones have none.
    assert out[0].text.startswith("Bitcoin story new") and out[2].text == ""


# --------------------------------------------------------------------------
# Scoring from the store
# --------------------------------------------------------------------------


def test_stored_items_the_feeds_dropped_still_count(store):
    earlier = NOW - timedelta(hours=40)
    news_store.with_store([_h(f"Bitcoin rally is great {i}", 2, now=earlier) for i in range(6)], earlier)
    # Now the feeds show something else entirely.
    payload = svc.build_payload("BTC", select_headlines(news_store.with_store([_h("Ethereum news", 1)], NOW), "BTC"), None, NOW)
    overall = payload["overall_sentiment"]
    assert overall["sample_size"] == 6 and overall["window"] == "72h-hl24-v3"
    assert all(d["link"] and set(d) == {"title", "source", "link", "published", "score", "weight", "effect"} for d in payload["drivers"])
    assert SECRET not in json.dumps(payload)


def test_same_weighting_and_effective_sample_as_live(store):
    items = [_h(f"Bitcoin rally is great {i}", 6 * i) for i in range(8)]
    live_only = svc.build_payload("BTC", items, None, NOW)
    via_store = svc.build_payload("BTC", select_headlines(news_store.with_store(items, NOW), "BTC"), None, NOW)
    for field in ("sample_size", "average_compound", "label", "unweighted_compound_72h", "effective_sample_size"):
        assert via_store["overall_sentiment"][field] == live_only["overall_sentiment"][field], field
    assert via_store["drivers"] == live_only["drivers"]


def test_stored_score_is_used_not_rescored(store):
    news_store.with_store([_h("Bitcoin rally is great", 1, link="https://x.test/s")], NOW)
    with _db() as conn:
        conn.execute("UPDATE news_items SET score = 0.123")
    [s] = window.in_window(select_headlines(news_store.with_store([], NOW), "BTC"), NOW)
    assert s.score == 0.123


def test_fetch_inputs_and_on_demand_calls_write_the_store(store, monkeypatch):
    async def fake_items(limit_per_feed=30):
        return [_h("Bitcoin rally", 1, now=datetime.now(UTC)), _h("Ethereum dips", 2, now=datetime.now(UTC))]

    async def fake_fng():
        return None

    monkeypatch.setattr(svc, "fetch_feed_items", fake_items)
    monkeypatch.setattr(svc, "fetch_fear_greed", fake_fng)
    payload = asyncio.run(svc.compute_sentiment_payload("BTC"))
    assert payload["overall_sentiment"]["sample_size"] == 1
    assert sorted(r["title"] for r in _rows()) == ["Bitcoin rally", "Ethereum dips"]


# --------------------------------------------------------------------------
# Expiry
# --------------------------------------------------------------------------


def test_purge_deletes_rows_past_72h_plus_margin(store):
    news_store.with_store([_h("Bitcoin a", 1), _h("Bitcoin b", 72.5), _h("Bitcoin c", 72.9)], NOW)
    later = NOW + timedelta(hours=0.5)  # b is now 73.0h old, c 73.4h
    assert news_store.purge_expired(later) == 1
    assert sorted(r["title"] for r in _rows()) == ["Bitcoin a", "Bitcoin b"]
    assert news_store.purge_expired(NOW + timedelta(hours=100)) == 2


def test_hourly_run_purges_and_records_v3(store, monkeypatch):
    with _db() as conn:
        conn.execute(
            "INSERT INTO news_items (outlet, title, link, published, first_seen, score, coins) "
            "VALUES ('CoinDesk', 'Bitcoin ancient', 'https://x.test/old', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00+00:00', 0.5, '[\"BTC\"]')"
        )

    async def fake_items(limit_per_feed=30):
        return [_h(f"Bitcoin rally is great {i}", i + 1, now=datetime.now(UTC)) for i in range(6)]

    async def fake_fng():
        return None

    monkeypatch.setattr(svc, "fetch_feed_items", fake_items)
    monkeypatch.setattr(svc, "fetch_fear_greed", fake_fng)
    asyncio.run(hourly.log_hour(["BTC"]))
    assert "Bitcoin ancient" not in [r["title"] for r in _rows()] and len(_rows()) == 6
    with _db() as conn:
        win, n = conn.execute('SELECT "window", sample_size FROM sentiment_hourly').fetchone()
        assert conn.execute("SELECT COUNT(*) FROM sentiment_headlines").fetchone()[0] == 6
    assert win == "72h-hl24-v3" and n == 6


# --------------------------------------------------------------------------
# Fallback
# --------------------------------------------------------------------------


def test_unwritable_store_falls_back_to_live_with_one_warning(store, monkeypatch, caplog):
    def broken():
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(billing, "_db", broken)
    live = [_h("Bitcoin rally", 1)]
    with caplog.at_level(logging.WARNING, logger="app.news_store"):
        out = news_store.with_store(live, NOW)
    assert out is live
    warnings = [r for r in caplog.records if r.name == "app.news_store"]
    assert len(warnings) == 1 and "scoring the live feeds only" in warnings[0].getMessage()
    assert warnings[0].exc_info is None  # one line, no traceback


def test_unreadable_table_falls_back_to_live(store, monkeypatch, caplog):
    with _db() as conn:
        conn.execute("DROP TABLE news_items")
        conn.execute("CREATE TABLE news_items (id INTEGER)")  # wrong shape: insert fails
    live = [_h("Bitcoin rally", 1)]
    with caplog.at_level(logging.WARNING, logger="app.news_store"):
        assert news_store.with_store(live, NOW) is live
    assert sum(r.name == "app.news_store" for r in caplog.records) == 1


def test_hourly_still_logs_when_the_store_is_broken(store, monkeypatch):
    def broken():
        raise sqlite3.OperationalError("disk I/O error")

    async def fake_items(limit_per_feed=30):
        return [_h(f"Bitcoin rally {i}", i + 1, now=datetime.now(UTC)) for i in range(6)]

    async def fake_fng():
        return None

    monkeypatch.setattr(svc, "fetch_feed_items", fake_items)
    monkeypatch.setattr(svc, "fetch_fear_greed", fake_fng)
    real_db = billing._db
    monkeypatch.setattr(news_store.billing, "_db", broken)
    monkeypatch.setattr(hourly, "_db", real_db)
    asyncio.run(hourly.log_hour(["BTC"]))
    with real_db() as conn:
        assert conn.execute("SELECT sample_size FROM sentiment_hourly").fetchone()[0] == 6


def test_disabled_store_is_live_only_and_silent(store, monkeypatch, caplog):
    monkeypatch.setattr(news_store, "ENABLED", False)
    live = [_h("Bitcoin rally", 1)]
    with caplog.at_level(logging.WARNING):
        assert news_store.with_store(live, NOW) is live
    assert _rows() == [] and not caplog.records


# --------------------------------------------------------------------------
# MATIC -> POL
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("POL rallies after Polygon upgrade", True),
        ("$POL breaks out", True),
        ("Polygon network update", True),
        ("MATIC holders migrate", True),
        ("$MATIC", True),
        ("POL", True),
        ("$POL", True),
        ("Polygon's POL token", True),
        ("PoL", False),
        ("pol", False),
        ("$pol", False),
        ("Berachain's Proof-of-Liquidity (PoL) model", False),
        ("Traders rotate into pol", False),
        ("matic", True),  # the ticker itself stays case-insensitive
        ("New policy on stablecoins", False),
        ("Poland's central bank", False),
        ("Liquidity pools grow", False),
        ("Political risk rises", False),
        ("Polkadot parachain news", False),
    ],
)
def test_pol_matches_matic(text, expected):
    assert mentions("MATIC", text) is expected


def test_pol_doesnt_match_other_coins():
    assert [s for s in COIN_NAMES if mentions(s, "POL jumps 10%")] == ["MATIC"]


def test_pol_input_symbol_resolves_to_matic():
    assert validate_symbol("POL") == validate_symbol(" pol ") == "MATIC"
    assert validate_symbol("MATIC") == "MATIC"
    assert "POL" not in COIN_NAMES  # supported list, OpenAPI enums and 402s unchanged
    assert integrations._normalize_tv_ticker("BINANCE:POLUSDT") == "POL"
    assert validate_symbol(integrations._normalize_tv_ticker("POLUSDT")) == "MATIC"


def test_pol_reading_is_answered_as_matic(monkeypatch):
    async def fake_items(limit_per_feed=30):
        return [_h(f"POL rallies {i}", i + 1, now=datetime.now(UTC)) for i in range(6)]

    async def fake_fng():
        return None

    monkeypatch.setattr(svc, "fetch_feed_items", fake_items)
    monkeypatch.setattr(svc, "fetch_fear_greed", fake_fng)
    payload = asyncio.run(svc.compute_sentiment_payload("pol"))
    assert payload["symbol"] == "MATIC" and payload["name"] == "Polygon"
    assert payload["overall_sentiment"]["sample_size"] == 6
    assert svc.require_supported_symbol("POL", "test") == "MATIC"


# --------------------------------------------------------------------------
# DOGE: no rule added (no Department of Government Efficiency matches in the
# 2026-11-27 measurement). This pins the current behavior so a change is
# deliberate.
# --------------------------------------------------------------------------


def test_doge_matching_is_unchanged():
    assert mentions("DOGE", "Dogecoin jumps")
    assert mentions("DOGE", "$DOGE pumps")
    assert mentions("DOGE", "Musk's DOGE cuts")  # no government-efficiency rule yet
