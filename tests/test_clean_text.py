"""
The 72h-hl24-v2 method change: feed text is cleaned before it is matched and
scored (HTML tags, entities, feed footers, the outlet's own name, a repeated
title), multi-word lexicon entries count, and the new version label is kept
apart from the old one everywhere (responses, hourly rows, leadlag).

Run with: python -m pytest
"""

import asyncio
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone

os.environ.setdefault("PAY_TO_ADDRESS", "0x000000000000000000000000000000000000dEaD")
os.environ.setdefault("CDP_API_KEY_ID", "test")
os.environ.setdefault("CDP_API_KEY_SECRET", "test")
os.environ.setdefault("BILLING_DB_PATH", os.path.join(tempfile.mkdtemp(), "billing.db"))

import httpx

from app import archive, window
from app import sentiment_service as svc
from app.coins import mentions
from app.sentiment import CRYPTO_LEXICON, _analyzer, score_text
from app.sources import news
from app.sources.news import Headline, clean_text

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import leadlag  # noqa: E402

UTC = timezone.utc


def _bm_desc(title, summary):
    """Shaped like Bitcoin Magazine's RSS descriptions: byline link, an image
    with inline CSS, the title again, the summary, then the "This post" footer."""
    return (
        '<p><a rel="nofollow" href="https://bitcoinmagazine.com">Bitcoin Magazine</a><br />\n'
        '<img src="https://bitcoinmagazine.com/x.jpg" style="display: block; margin: 1em auto"><br />\n'
        f'<a rel="nofollow" href="https://bitcoinmagazine.com/news/x">{title}</a></p>\n'
        f"<p>{summary} </p>\n"
        f'<p>This post <a rel="nofollow" href="https://bitcoinmagazine.com/news/x">{title}</a> first appeared on '
        '<a rel="nofollow" href="https://bitcoinmagazine.com">Bitcoin Magazine</a> and is written by '
        '<a rel="nofollow" href="https://bitcoinmagazine.com/authors/a">A Writer</a>.</p>'
    )


TITLE = "Bitcoin Nears $85,000 as Earnings Lift Stocks"
BM_DESC = _bm_desc(TITLE, "The price is rising with the stock market.")
OFF_TOPIC_TITLE = "SEC Opens Tokenized Stock Filings"
OFF_TOPIC_DESC = _bm_desc(OFF_TOPIC_TITLE, "Tokenized stocks are rising.")


# --------------------------------------------------------------------------
# Cleaning
# --------------------------------------------------------------------------


def test_html_tags_and_entities_are_removed():
    text = clean_text(
        "SEC&#8217;s plan &amp; Ether",
        '<p style="float:right"><img src="https://x.test/a.jpg" alt="SEC plan" style="display: block"></p>'
        "<p>Regulators&#160;weigh it &lt;today&gt;.</p>",
        "Cointelegraph",
    )
    assert text == "SEC’s plan & Ether Regulators weigh it <today>."
    assert "block" not in text and "img" not in text


def test_markup_is_not_scored():
    # Before: the CSS "display: block" (VADER -1.9) made this negative.
    assert score_text(clean_text(TITLE, BM_DESC, "Bitcoin Magazine")) == score_text(
        f"{TITLE} The price is rising with the stock market."
    )
    assert score_text(f"{TITLE} {BM_DESC}") < 0


def test_footers_are_dropped():
    wordpress = clean_text("Title", "<p>Body text.</p><p>The post <a href='x'>Title</a> appeared first on <a href='y'>Some Site</a>.</p>")
    bitcoin_magazine = clean_text(TITLE, BM_DESC, "Bitcoin Magazine")
    cryptoslate = clean_text(
        "Title",
        '<p>Body text.</p>\n<p><a href="https://cryptoslate.com/x/">Read the full story on CryptoSlate</a></p>'
        '<p>Understand the crypto stories that matter and what to watch next. '
        '<a href="https://cryptoslate.com/newsletters/">Get The Catalyst in your inbox</a></p>',
        "CryptoSlate",
    )
    assert wordpress == "Title Body text."
    assert bitcoin_magazine == f"{TITLE} The price is rising with the stock market."
    assert cryptoslate == "Title Body text."
    # A sentence that merely contains "the post" stays.
    assert clean_text("T", "Analysts said the post-halving rally held.") == "T Analysts said the post-halving rally held."


def test_outlet_byline_is_not_a_bitcoin_mention():
    text = clean_text(OFF_TOPIC_TITLE, OFF_TOPIC_DESC, "Bitcoin Magazine")
    assert text == f"{OFF_TOPIC_TITLE} Tokenized stocks are rising."
    assert not mentions("BTC", text)
    assert mentions("BTC", f"{OFF_TOPIC_TITLE} {OFF_TOPIC_DESC}")  # what the byline did before
    # A real mention in the summary still counts.
    on_topic = _bm_desc(OFF_TOPIC_TITLE, "Bitcoin is rising.")
    assert mentions("BTC", clean_text(OFF_TOPIC_TITLE, on_topic, "Bitcoin Magazine"))


def test_fetched_items_carry_clean_text(monkeypatch):
    now = datetime.now(UTC).strftime("%a, %d %b %Y %H:%M:%S +0000")
    rss = (
        f"<rss><channel><item><title>{OFF_TOPIC_TITLE}</title><link>https://x.test/a</link>"
        f"<pubDate>{now}</pubDate><description><![CDATA[{OFF_TOPIC_DESC}]]></description></item></channel></rss>"
    )
    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        return real(*args, transport=httpx.MockTransport(lambda r: httpx.Response(200, content=rss.encode())), **kwargs)

    monkeypatch.setattr(news.httpx, "AsyncClient", factory)
    monkeypatch.setattr(news, "RSS_FEEDS", [news.NEWS_OUTLETS["Bitcoin Magazine"]])
    monkeypatch.setattr(news, "FEED_HEALTH", {})
    (item,) = asyncio.run(news.fetch_feed_items())
    assert "<" not in item.text and "Bitcoin Magazine" not in item.text and "first appeared" not in item.text
    assert news.select_headlines([item], "BTC") == []


# --------------------------------------------------------------------------
# Multi-word lexicon entries
# --------------------------------------------------------------------------


def test_multi_word_lexicon_entries_score():
    assert {k for k in CRYPTO_LEXICON if " " in k} == {"bull run", "rug pull"}
    # "bull" and "run" aren't in VADER's lexicon, so this scored 0 before.
    assert _analyzer.polarity_scores("Bitcoin starts a bull run")["compound"] == 0
    assert score_text("Bitcoin starts a bull run") == _analyzer.polarity_scores("Bitcoin starts a bull_run")["compound"] > 0
    assert score_text("Bitcoin starts a BULL  RUN") > score_text("Bitcoin starts a bull run")  # caps still count
    assert score_text("Devs pulled a rug pull") == _analyzer.polarity_scores("Devs pulled a rug_pull")["compound"] < 0
    assert score_text("a bullrun") == _analyzer.polarity_scores("a bullrun")["compound"]  # whole words only


def test_no_lexicon_words_or_valences_changed():
    for word, valence in CRYPTO_LEXICON.items():
        assert _analyzer.lexicon[word] == valence
        if " " in word:
            assert _analyzer.lexicon[word.replace(" ", "_")] == valence


# --------------------------------------------------------------------------
# Version label
# --------------------------------------------------------------------------


def test_responses_carry_the_new_version():
    now = datetime.now(UTC)
    items = [
        Headline("Bitcoin rally", "CoinDesk", None, (now - timedelta(hours=h)).isoformat(), "Bitcoin rally")
        for h in range(6)
    ]
    payload = svc.build_payload("BTC", items, None, now)
    assert payload["overall_sentiment"]["window"] == window.WINDOW_VERSION == "72h-hl24-v2"
    # The paid route's advertised example is unchanged.
    assert archive.HISTORY_EXAMPLE["rows"][1]["window"] == "72h-hl24"


def test_leadlag_selects_one_method_version(tmp_path):
    path = tmp_path / "ll.db"
    conn = sqlite3.connect(path)
    conn.execute(
        'CREATE TABLE sentiment_hourly (symbol TEXT, observed_at TEXT, average_compound REAL, matcher TEXT, '
        'unweighted_compound_72h REAL, "window" TEXT)'
    )
    conn.executemany(
        "INSERT INTO sentiment_hourly VALUES (?, ?, ?, ?, ?, ?)",
        [
            ("BTC", "2026-10-03T22:00:02+00:00", 0.1, "whole_word", None, None),
            ("BTC", "2026-10-20T13:00:02+00:00", 0.2, "whole_word", 0.25, "72h-hl24"),
            ("BTC", "2026-10-22T13:00:02+00:00", 0.3, "whole_word", 0.35, "72h-hl24-v2"),
        ],
    )
    conn.commit()
    conn.close()
    load = lambda **kw: sorted(leadlag.load_sentiment(str(path), "BTC", **kw).values())  # noqa: E731
    assert load(window="72h-hl24") == load(window_only=True) == [0.2]
    assert load(window="72h-hl24-v2") == [0.3]
    assert load(window="72h-hl24-v2", window_only=True) == [0.3]  # an explicit version wins
    assert load() == [0.1, 0.2, 0.3]
