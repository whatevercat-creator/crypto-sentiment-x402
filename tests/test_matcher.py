"""
Whole-word headline matching (app/coins.py), the single-fetch hourly log
(app/hourly.py), the DL News retry (app/sources/news.py), lead/lag's
whole-word-only filter, and the 2026-10-03 methodology note.

Run with: python -m pytest
"""

import asyncio
import json
import os
import sqlite3
import sys
import tempfile

os.environ.setdefault("PAY_TO_ADDRESS", "0x000000000000000000000000000000000000dEaD")
os.environ.setdefault("CDP_API_KEY_ID", "test")
os.environ.setdefault("CDP_API_KEY_SECRET", "test")
os.environ.setdefault("BILLING_DB_PATH", os.path.join(tempfile.mkdtemp(), "billing.db"))

import httpx
import pytest

from app import hourly
from app.billing import _db
from app.coins import COIN_NAMES, ORIGINAL_SYMBOLS, match_pattern, mentions
from app.sources import news

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import leadlag  # noqa: E402


# --------------------------------------------------------------------------
# Matcher
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "symbol,text",
    [
        ("ETH", "Ethereum's fee market"),
        ("ETH", "ETH breaks $3K"),
        ("ETH", "eth funding flips"),
        ("ETH", "Ether rallies"),
        ("ETH", "Traders pile into $ETH"),
        ("BTC", "Bitcoin nears highest level"),
        ("BTC", "Miners sold bitcoins"),
        ("SOL", "Bull targets for BTC, ETH, XRP, and SOL"),
        ("LINK", "Chainlink built a Swift integration"),
        ("LINK", "$LINK jumps 12%"),
        ("NEAR", "NEAR Intents hit by $3.8 million exploit"),
        ("NEAR", "NEAR Protocol upgrade"),
        ("NEAR", "$NEAR rebounds"),
        ("QNT", "Quant (QNT) could explode"),
        ("PUMP", "Pump.fun buys back tokens"),
        ("HYPE", "Hyperliquid's USDC yield program"),
        ("USDC", "USD Coin supply grows"),
        ("USDT", "Tether mints $1B"),
    ],
)
def test_matches_real_mentions(symbol, text):
    assert mentions(symbol, text)


@pytest.mark.parametrize(
    "symbol,text",
    [
        ("ETH", "The review centers on whether Binance can rely on it"),
        ("ETH", "Tether's USDT is coming home"),
        ("ETH", "working together on ethics"),
        ("ETH", "SMBC Nikko and Nethermind plan hooks"),
        ("ETH", "Vitalik-backed MegaETH sees token fall"),
        ("BTC", "Read more on NewsBTC"),
        ("SOL", "a Swift Hackathon solution; exit rules unresolved"),
        ("LINK", "accounts linked to a hack; follow the link"),
        ("DOT", "connect the dots"),
        ("NEAR", "bitcoin dominance nears 60%, near-term risk, nearly done"),
        ("QNT", "quant funds and enormous quantities of data"),
        ("PUMP", "SUI may be gearing up for a huge pump"),
        ("HYPE", "will it live up to the hype?"),
        ("ENA", "key senators stalled it in the Senate; OpenAI fires staff"),
        ("TAO", "the tao of trading"),
        ("ADA", "a USB-C adapter for the Armada"),
        ("MATIC", "settles automatically"),
        ("LTC", "altcoins surged"),
    ],
)
def test_rejects_substring_false_positives(symbol, text):
    assert not mentions(symbol, text)


def test_html_link_urls_dont_count():
    text = 'Rates hold <a href="https://bitcoinmagazine.com/markets/bitcoin-price">read</a>'
    assert not mentions("BTC", text)
    assert mentions("BTC", text + " as Bitcoin stalls")


def test_case_insensitive_and_every_coin_has_a_pattern():
    assert mentions("ETH", "ETHEREUM")
    for symbol in COIN_NAMES:
        assert match_pattern(symbol).pattern
    assert mentions("ZZZ", "ZZZ token lists")  # unknown ticker: ticker only


def test_new_names_and_dataset_default_unchanged():
    for symbol, name in {
        "NEAR": "NEAR Protocol", "HYPE": "Hyperliquid", "QNT": "Quant", "AAVE": "Aave",
        "TAO": "Bittensor", "PEPE": "Pepe", "PUMP": "Pump.fun", "ENA": "Ethena", "STRK": "Starknet",
    }.items():
        assert COIN_NAMES[symbol] == name
    for symbol in ("AAVE", "PEPE", "ENA", "STRK"):
        assert mentions(symbol, f"{COIN_NAMES[symbol]} news")
    from app import dataset

    if not os.environ.get("SNAPSHOT_SYMBOLS"):
        assert dataset.SNAPSHOT_SYMBOLS == list(ORIGINAL_SYMBOLS)
    assert len(ORIGINAL_SYMBOLS) == 15


# --------------------------------------------------------------------------
# Feed fetch: one retry on 5xx (DL News 504s)
# --------------------------------------------------------------------------

RSS = b"""<rss><channel>
<item><title>Ether rallies</title><description>whether or not</description></item>
<item><title>Solana and XRP climb</title><description></description></item>
<item><title>Tether update</title><description>together</description></item>
</channel></rss>"""


def _patch_client(monkeypatch, handler):
    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        return real(*args, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(news.httpx, "AsyncClient", factory)
    monkeypatch.setattr(news, "RETRY_DELAY_SECONDS", 0)


def test_feed_retried_once_after_5xx(monkeypatch):
    calls = {}

    def handler(request):
        n = calls[str(request.url)] = calls.get(str(request.url), 0) + 1
        if "dlnews" in request.url.host and n == 1:
            return httpx.Response(504)
        return httpx.Response(200, content=RSS)

    _patch_client(monkeypatch, handler)
    items = asyncio.run(news.fetch_feed_items())
    assert len(items) == 3 * len(news.RSS_FEEDS)
    assert calls["https://www.dlnews.com/arc/outboundfeeds/rss/"] == 2
    assert all(n == 1 for url, n in calls.items() if "dlnews" not in url)


def test_feed_skipped_after_second_5xx_and_not_retried_on_4xx(monkeypatch):
    calls = {}

    def handler(request):
        calls[request.url.host] = calls.get(request.url.host, 0) + 1
        if "dlnews" in request.url.host:
            return httpx.Response(504)
        if "decrypt" in request.url.host:
            return httpx.Response(404)
        return httpx.Response(200, content=RSS)

    _patch_client(monkeypatch, handler)
    items = asyncio.run(news.fetch_feed_items())
    assert len(items) == 3 * (len(news.RSS_FEEDS) - 2)
    assert calls["www.dlnews.com"] == 2 and calls["decrypt.co"] == 1


def _h(text):
    return news.Headline(title=text, source="Test", link=None, published=None, text=text)


def test_select_headlines_whole_word():
    items = [_h("Ether rallies whether or not"), _h("Tether update together")]
    assert news.select_headlines(items, "ETH") == [items[0]]


# --------------------------------------------------------------------------
# Hourly: one fetch for all symbols
# --------------------------------------------------------------------------


@pytest.fixture
def fresh_hourly_db(monkeypatch, tmp_path):
    import app.billing as billing

    monkeypatch.setattr(billing, "DB_PATH", str(tmp_path / "h.db"), raising=False)
    path = str(tmp_path / "h.db")
    monkeypatch.setenv("BILLING_DB_PATH", path)
    hourly.init_hourly_db()
    yield


def test_hourly_fetches_feeds_once_for_all_symbols(monkeypatch, fresh_hourly_db):
    fetches = {"feeds": 0, "fng": 0}

    async def fake_items(limit_per_feed=30):
        fetches["feeds"] += 1
        return [_h(t) for t in ("Ether rallies", "Solana and XRP climb", "Bitcoin dips", "Tether whether")]

    async def fake_fng():
        fetches["fng"] += 1
        return {"value": 40, "classification": "Fear"}

    import app.sentiment_service as svc

    monkeypatch.setattr(svc, "fetch_feed_items", fake_items)
    monkeypatch.setattr(svc, "fetch_fear_greed", fake_fng)
    with _db() as conn:
        conn.execute("DELETE FROM sentiment_hourly")

    asyncio.run(hourly.log_hour(["BTC", "ETH", "XRP", "SOL"]))
    assert fetches == {"feeds": 1, "fng": 1}

    with _db() as conn:
        rows = conn.execute(
            "SELECT symbol, observed_at, sample_size, fear_greed_value, matcher FROM sentiment_hourly"
        ).fetchall()
    by_symbol = {r[0]: r for r in rows}
    assert set(by_symbol) == {"BTC", "ETH", "XRP", "SOL"}
    assert len({r[1] for r in rows}) == 1  # same observed_at for every symbol
    assert by_symbol["ETH"][2] == 1  # "Tether whether" is not ETH
    assert all(r[3] == 40 and r[4] == "whole_word" for r in rows)

    # Same hour again: nothing pending, no fetch.
    asyncio.run(hourly.log_hour(["BTC", "ETH", "XRP", "SOL"]))
    assert fetches == {"feeds": 1, "fng": 1}


def test_init_adds_matcher_column_to_existing_table(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE sentiment_hourly (id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL, "
        "observed_at TEXT NOT NULL, average_compound REAL, sample_size INTEGER, fear_greed_value INTEGER)"
    )
    conn.execute("INSERT INTO sentiment_hourly (symbol, observed_at, average_compound) VALUES ('BTC', '2026-10-01T00:00:01+00:00', 0.1)")
    conn.commit()
    conn.close()
    monkeypatch.setenv("BILLING_DB_PATH", str(path))
    import app.billing as billing

    monkeypatch.setattr(billing, "DB_PATH", str(path), raising=False)
    hourly.init_hourly_db()
    hourly.init_hourly_db()  # idempotent
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT matcher FROM sentiment_hourly").fetchall() == [(None,)]
    conn.close()


# --------------------------------------------------------------------------
# leadlag: only readings after the change
# --------------------------------------------------------------------------


def test_leadlag_whole_word_only_and_since(tmp_path):
    path = tmp_path / "ll.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE sentiment_hourly (symbol TEXT, observed_at TEXT, average_compound REAL, "
        "sample_size INTEGER, fear_greed_value INTEGER, matcher TEXT)"
    )
    rows = [
        ("ETH", "2026-10-02T22:00:02+00:00", 0.1, None),
        ("ETH", "2026-10-02T23:00:02+00:00", 0.2, None),
        ("ETH", "2026-10-03T15:00:02+00:00", 0.3, "whole_word"),
        ("ETH", "2026-10-03T16:00:02+00:00", 0.4, "whole_word"),
    ]
    conn.executemany(
        "INSERT INTO sentiment_hourly (symbol, observed_at, average_compound, matcher) VALUES (?, ?, ?, ?)", rows
    )
    conn.commit()
    conn.close()

    assert len(leadlag.load_sentiment(str(path), "ETH")) == 4
    ww = leadlag.load_sentiment(str(path), "ETH", whole_word_only=True)
    assert sorted(v for v in ww.values()) == [0.3, 0.4]
    since = leadlag.parse_ts("2026-10-02T23:00:00Z")
    assert len(leadlag.load_sentiment(str(path), "ETH", since=since)) == 3


def test_leadlag_whole_word_only_on_pre_change_db(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE sentiment_hourly (symbol TEXT, observed_at TEXT, average_compound REAL)")
    conn.execute("INSERT INTO sentiment_hourly VALUES ('BTC', '2026-10-01T00:00:01+00:00', 0.1)")
    conn.commit()
    conn.close()
    assert leadlag.load_sentiment(str(path), "BTC", whole_word_only=True) == {}


# --------------------------------------------------------------------------
# Methodology note
# --------------------------------------------------------------------------


def test_methodology_note_on_validation_and_transparency():
    from app import main

    with open(os.path.join(os.path.dirname(main.__file__), "validation.json")) as f:
        doc = json.load(f)
    [note] = doc["methodology_changes"]
    assert note["date"] == "2026-10-03"
    assert "substring" in note["before"]
    assert "--whole-word-only" in doc["method"]

    transparency = asyncio.run(main.transparency())
    assert transparency["methodology_changes"] == doc["methodology_changes"]
