"""
`drivers` in the sentiment response: the up to 5 headlines that moved the
score most (title, source, link, published, own score -- never article
text), `drivers_summary`, and the top 3 stored with each hourly row.

Run with: python -m pytest
"""

import asyncio
import json
import os
import sqlite3
import tempfile

os.environ.setdefault("PAY_TO_ADDRESS", "0x000000000000000000000000000000000000dEaD")
os.environ.setdefault("CDP_API_KEY_ID", "test")
os.environ.setdefault("CDP_API_KEY_SECRET", "test")
os.environ.setdefault("BILLING_DB_PATH", os.path.join(tempfile.mkdtemp(), "billing.db"))

import httpx
import pytest

from app import hourly, main
from app import sentiment_service as svc
from app.sentiment import score_text
from app.sources import news
from app.sources.news import Headline

SECRET = "SECRET-ARTICLE-BODY"


def _h(title, desc="", source="CoinDesk", link=None, published=None):
    return Headline(title=title, source=source, link=link, published=published, text=f"{title} {desc}")


HEADLINES = [
    _h("Bitcoin holds steady", "Prices were flat. " + SECRET, link="https://example.com/1"),
    _h("Bitcoin exchange hacked in massive scam", SECRET, source="The Block"),
    _h("Bitcoin rally is great, bullish breakout", SECRET),
    _h("Bitcoin miners worried about weak fees", SECRET),
    _h("Bitcoin ETF approval is good news", SECRET),
    _h("Bitcoin crash fears as traders panic", SECRET),
    _h("Bitcoin conference opens in Lisbon", SECRET),
]


# --------------------------------------------------------------------------
# top_drivers / drivers_summary
# --------------------------------------------------------------------------


def test_drivers_are_largest_absolute_scores_first_without_article_text():
    drivers = svc.top_drivers(HEADLINES)
    assert 0 < len(drivers) <= 5
    scores = [d["score"] for d in drivers]
    assert [abs(s) for s in scores] == sorted((abs(s) for s in scores), reverse=True)
    expected = sorted(
        (abs(score_text(h.text[:500])) for h in HEADLINES if score_text(h.text[:500]) != 0), reverse=True
    )[:5]
    assert [abs(s) for s in scores] == [round(s, 4) for s in expected]
    assert all(set(d) == {"title", "source", "link", "published", "score"} for d in drivers)
    assert SECRET not in json.dumps(drivers)
    assert all(d["score"] != 0 for d in drivers)


def test_drivers_score_is_the_headlines_own_score():
    [driver] = svc.top_drivers([_h("Bitcoin crash fears as traders panic", "more text")])
    assert driver["score"] == round(score_text("Bitcoin crash fears as traders panic more text"), 4)
    assert driver["title"] == "Bitcoin crash fears as traders panic"


@pytest.mark.parametrize(
    "scores,expected",
    [
        ([], "No current headlines about BTC moved the score."),
        ([-0.5], "The top headline is negative."),
        ([0.5, 0.3, 0.2], "All 3 top headlines are positive."),
        ([-0.6, -0.5, -0.4, 0.3, 0.2], "3 of the top 5 headlines are negative, 2 are positive."),
        ([-0.6, 0.5, 0.4, 0.03], "2 of the top 4 headlines are positive, 1 is negative, 1 is neutral."),
        ([0.6, -0.5], "1 of the top 2 headlines is positive, 1 is negative."),
    ],
)
def test_drivers_summary(scores, expected):
    assert svc.drivers_summary("BTC", [{"score": s} for s in scores]) == expected


def test_payload_has_drivers_and_unchanged_score():
    payload = svc.build_payload("BTC", HEADLINES, None)
    assert payload["drivers"] == svc.top_drivers(HEADLINES)
    assert payload["drivers_summary"] == svc.drivers_summary("BTC", payload["drivers"])
    # The overall score is still the average over every matched headline.
    expected = sum(score_text(h.text[:500]) for h in HEADLINES) / len(HEADLINES)
    assert payload["overall_sentiment"]["average_compound"] == round(expected, 4)
    assert payload["overall_sentiment"]["sample_size"] == len(HEADLINES)


# --------------------------------------------------------------------------
# Feed parsing: title, source, link, published
# --------------------------------------------------------------------------

RSS = b"""<rss><channel>
<item><title><![CDATA[Bitcoin &#8217;s <b>big</b> day]]></title>
<link>https://news.test/a</link><pubDate>Fri, 03 Oct 2026 14:05:00 +0200</pubDate>
<description>SECRET-ARTICLE-BODY</description></item>
<item><title>Bitcoin dips</title><link>javascript:alert(1)</link><pubDate>not a date</pubDate></item>
</channel></rss>"""


def test_feed_items_carry_title_source_link_published(monkeypatch):
    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        return real(*args, transport=httpx.MockTransport(lambda r: httpx.Response(200, content=RSS)), **kwargs)

    monkeypatch.setattr(news.httpx, "AsyncClient", factory)
    monkeypatch.setattr(news, "RSS_FEEDS", [news.NEWS_OUTLETS["Decrypt"]])
    first, second = asyncio.run(news.fetch_feed_items())
    assert first.title == "Bitcoin ’s big day"
    assert first.source == "Decrypt"
    assert first.link == "https://news.test/a"
    assert first.published == "2026-10-03T12:05:00Z"
    assert SECRET in first.text  # scored, never returned
    assert second.link is None and second.published is None


# --------------------------------------------------------------------------
# Routes, Bazaar, OpenAPI, llms.txt, home page
# --------------------------------------------------------------------------


@pytest.fixture
def client(monkeypatch):
    from fastapi.testclient import TestClient
    from x402.schemas import SupportedKind, SupportedResponse

    from app.billing import init_db
    from app.dataset import init_dataset_db

    monkeypatch.setattr(
        main.facilitator,
        "get_supported",
        lambda: SupportedResponse(kinds=[SupportedKind(x402Version=2, scheme="exact", network=main.CAIP2_NETWORK)]),
    )
    init_db()
    init_dataset_db()

    async def fake_headlines(symbol, name=""):
        return HEADLINES

    async def fake_fng():
        return {"value": 50, "classification": "Neutral"}

    monkeypatch.setattr(svc, "fetch_news_headlines", fake_headlines)
    monkeypatch.setattr(svc, "fetch_fear_greed", fake_fng)
    return TestClient(main.app)


def test_v1_route_returns_drivers(client, monkeypatch):
    monkeypatch.setattr(
        main, "verify_and_charge_api_key", lambda key: {"tier": "pro", "calls_used": 1, "limit": 100}
    )
    body = client.get("/v1/sentiment/BTC", headers={"X-API-Key": "k"}).json()
    assert body["drivers"] == svc.top_drivers(HEADLINES)
    assert body["drivers_summary"]
    assert SECRET not in json.dumps(body)


def test_x402_route_handler_returns_drivers(monkeypatch):
    async def headlines(symbol, name=""):
        return HEADLINES

    async def fng():
        return None

    monkeypatch.setattr(svc, "fetch_news_headlines", headlines)
    monkeypatch.setattr(svc, "fetch_fear_greed", fng)
    body = json.loads(asyncio.run(main.get_sentiment("BTC")).body)
    assert len(body["drivers"]) == 5 and "drivers_summary" in body
    assert SECRET not in json.dumps(body)


def test_bazaar_example_and_schema_include_drivers():
    import jsonschema

    output = main.SENTIMENT_DISCOVERY["bazaar"]["info"]["output"]
    assert output["example"]["drivers"] and output["example"]["drivers_summary"]
    output_schema = main.SENTIMENT_DISCOVERY["bazaar"]["schema"]["properties"]["output"]
    example_schema = output_schema["properties"]["example"]
    assert example_schema["properties"]["drivers"] == main.DRIVERS_SCHEMA
    jsonschema.validate(output["example"], example_schema)
    for d in output["example"]["drivers"]:
        assert d["link"].startswith("https://example.com/")
    assert len(main.routes["GET /sentiment/:symbol"].description) <= 500


def test_openapi_llms_and_home_describe_drivers(client):
    spec = client.get("/openapi.json").json()
    for path in ("/sentiment/{symbol}", "/v1/sentiment/{symbol}"):
        ok = spec["paths"][path]["get"]["responses"]["200"]
        assert "drivers" in ok["description"]
        assert ok["content"]["application/json"]["example"]["drivers"]
    llms = client.get("/llms.txt").text
    assert "drivers" in llms and "never article text" in llms
    html = client.get("/", headers={"Accept": "text/html"}).text
    assert "drivers_summary" in html and "headlines that moved the" in html


# --------------------------------------------------------------------------
# Hourly: top 3 drivers stored, old rows stay empty
# --------------------------------------------------------------------------


def test_hourly_stores_top_three_drivers(monkeypatch, tmp_path):
    import app.billing as billing

    path = tmp_path / "h.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE sentiment_hourly (id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL, "
        "observed_at TEXT NOT NULL, average_compound REAL, sample_size INTEGER, fear_greed_value INTEGER)"
    )
    conn.execute(
        "INSERT INTO sentiment_hourly (symbol, observed_at, average_compound) "
        "VALUES ('BTC', '2026-10-01T00:00:01+00:00', 0.1)"
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(billing, "DB_PATH", str(path))
    hourly.init_hourly_db()

    async def fake_items(limit_per_feed=30):
        return HEADLINES

    async def fake_fng():
        return None

    monkeypatch.setattr(svc, "fetch_feed_items", fake_items)
    monkeypatch.setattr(svc, "fetch_fear_greed", fake_fng)
    asyncio.run(hourly.log_hour(["BTC"]))

    conn = sqlite3.connect(path)
    rows = conn.execute("SELECT observed_at, drivers FROM sentiment_hourly ORDER BY id").fetchall()
    conn.close()
    assert rows[0][1] is None  # existing row stays empty
    stored = json.loads(rows[1][1])
    assert stored == svc.top_drivers(HEADLINES)[:3]
    assert SECRET not in rows[1][1]
