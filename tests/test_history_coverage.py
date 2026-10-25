"""
Headline coverage: /history refuses (404, not charged) a range in which
every hour had zero matching headlines, and /archive, the home page and
llms.txt report each symbol's share of the last 7 days' hours that had any
(coverage_7d_pct).

Run with: python -m pytest
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone

from tests.test_archive import client, db  # noqa: F401  (shared fixtures)
from app import archive, main
from app.billing import _db

UTC = timezone.utc


def _insert(symbol, observed_at, sample_size, compound=0.0):
    with _db() as conn:
        conn.execute(
            "INSERT INTO sentiment_hourly (symbol, observed_at, average_compound, sample_size, "
            "fear_greed_value, matcher) VALUES (?, ?, ?, ?, 50, 'whole_word')",
            (symbol, observed_at.isoformat(), compound, sample_size),
        )


def _history(symbol, start, end):
    return asyncio.run(main.get_history(symbol, start=start, end=end))


# --------------------------------------------------------------------------
# GET /history/{symbol}: all-zero ranges
# --------------------------------------------------------------------------


def test_all_zero_range_is_404_with_coverage(db):  # noqa: F811
    now = datetime.now(UTC).replace(minute=0, second=2, microsecond=0)
    # 3 zero-headline hours in the requested range, and one hour with
    # headlines earlier in the week, so coverage is 1 of 4 logged hours.
    _insert("AVAX", now - timedelta(days=3), sample_size=2, compound=0.2)
    for i in (3, 2, 1):
        _insert("AVAX", now - timedelta(hours=i), sample_size=0)
    start = archive._iso(now - timedelta(hours=4))
    end = archive._iso(now)

    r = _history("avax", start, end)
    assert r.status_code == 404
    assert json.loads(r.body) == {
        "error": "no_headlines_in_range",
        "symbol": "AVAX",
        "start": start,
        "end": end,
        "rows_in_range": 3,
        "coverage_7d_pct": 25.0,
        "message": "Every hour in this range had zero matching headlines for AVAX, so "
        "there's no sentiment to sell and nothing was charged. See GET /archive for coverage.",
    }


def test_mixed_range_is_200_with_every_row(db):  # noqa: F811
    _insert("DOT", datetime(2026, 10, 9, 21, 0, 2, tzinfo=UTC), sample_size=0)
    _insert("DOT", datetime(2026, 10, 9, 22, 0, 2, tzinfo=UTC), sample_size=1, compound=0.4)
    _insert("DOT", datetime(2026, 10, 9, 23, 0, 2, tzinfo=UTC), sample_size=0)
    r = _history("DOT", "2026-10-09T00:00:00Z", "2026-10-10T00:00:00Z")
    assert r.status_code == 200
    body = json.loads(r.body)
    assert body["count"] == 3
    assert [row["sample_size"] for row in body["rows"]] == [0, 1, 0]


def test_empty_range_still_gets_no_readings_404(db):  # noqa: F811
    _insert("DOT", datetime(2026, 10, 12, 7, 0, 2, tzinfo=UTC), sample_size=0)
    r = _history("DOT", "2026-10-01T00:00:00Z", "2026-10-10T00:00:00Z")
    assert r.status_code == 404 and json.loads(r.body)["error"] == "no_readings_in_range"


def test_null_sample_size_is_not_zero():
    assert archive.no_headlines([{"sample_size": 0}, {"sample_size": 0}])
    assert not archive.no_headlines([{"sample_size": 0}, {"sample_size": None}])
    assert not archive.no_headlines([])


def test_unpaid_all_zero_range_still_gets_the_402(client):  # noqa: F811
    _insert("AVAX", datetime.now(UTC) - timedelta(hours=1), sample_size=0)
    assert client.get("/history/AVAX").status_code == 402


# --------------------------------------------------------------------------
# coverage_7d
# --------------------------------------------------------------------------


def test_coverage_math(db):  # noqa: F811
    now = datetime(2026, 10, 25, 12, 30, tzinfo=UTC)
    base = now.replace(minute=0, second=2)
    for i, n in enumerate([5, 0, 3, 0]):  # BTC: 2 of 4 hours had headlines
        _insert("BTC", base - timedelta(hours=i), sample_size=n)
    _insert("ETH", base, sample_size=0)  # ETH: 0 of 1
    _insert("LTC", now - timedelta(days=8), sample_size=4)  # LTC: only older than 7 days
    _insert("SOL", base - timedelta(days=7) + timedelta(hours=1), sample_size=1)  # inside the window

    cov = archive.coverage_7d(now)
    assert cov["BTC"] == {"hours_with_headlines_7d": 2, "hours_logged_7d": 4, "coverage_7d_pct": 50.0}
    assert cov["ETH"] == {"hours_with_headlines_7d": 0, "hours_logged_7d": 1, "coverage_7d_pct": 0.0}
    assert cov["SOL"]["coverage_7d_pct"] == 100.0
    assert "LTC" not in cov
    assert archive.coverage_for("LTC", cov) == {
        "hours_with_headlines_7d": 0,
        "hours_logged_7d": 0,
        "coverage_7d_pct": None,
    }


def test_coverage_counts_hours_not_rows(db):  # noqa: F811
    now = datetime(2026, 10, 25, 12, 30, tzinfo=UTC)
    # Two rows in one hour (a restart, say): one hour logged, with headlines.
    _insert("BTC", now.replace(minute=0, second=2), sample_size=0)
    _insert("BTC", now.replace(minute=5), sample_size=3)
    assert archive.coverage_7d(now)["BTC"] == {
        "hours_with_headlines_7d": 1, "hours_logged_7d": 1, "coverage_7d_pct": 100.0,
    }


def test_coverage_pct_rounds_to_one_decimal(db):  # noqa: F811
    now = datetime(2026, 10, 25, 12, 30, tzinfo=UTC)
    for i in range(3):
        _insert("BTC", now.replace(minute=0, second=2) - timedelta(hours=i), sample_size=1 if i == 0 else 0)
    assert archive.coverage_7d(now)["BTC"]["coverage_7d_pct"] == 33.3


# --------------------------------------------------------------------------
# Where it's shown
# --------------------------------------------------------------------------


def test_archive_json_has_coverage_fields(client):  # noqa: F811
    now = datetime.now(UTC).replace(minute=0, second=2, microsecond=0)
    _insert("BTC", now - timedelta(hours=1), sample_size=10)
    _insert("BTC", now - timedelta(hours=2), sample_size=0)
    _insert("QNT", now - timedelta(days=10), sample_size=0)  # nothing in the last 7 days
    body = client.get("/archive").json()
    assert body["symbols"]["BTC"]["hours_with_headlines_7d"] == 1
    assert body["symbols"]["BTC"]["hours_logged_7d"] == 2
    assert body["symbols"]["BTC"]["coverage_7d_pct"] == 50.0
    assert body["symbols"]["QNT"]["hours_logged_7d"] == 0
    assert body["symbols"]["QNT"]["coverage_7d_pct"] is None
    assert "coverage_7d_pct" in body["coverage_note"]


def test_home_page_lists_coverage(client):  # noqa: F811
    now = datetime.now(UTC).replace(minute=0, second=2, microsecond=0)
    _insert("BTC", now - timedelta(hours=1), sample_size=10)
    _insert("AVAX", now - timedelta(hours=1), sample_size=0)
    html = client.get("/", headers={"Accept": "text/html"}).text
    assert "BTC 100%" in html and "AVAX 0%" in html and "ETH n/a" in html
    assert "insufficient recent news&rdquo; (not charged)" in html


def test_home_page_without_coverage_data_omits_the_line(client):  # noqa: F811
    html = client.get("/", headers={"Accept": "text/html"}).text
    assert "Hours with matching headlines" not in html


def test_llms_and_openapi_describe_the_refusal(client):  # noqa: F811
    assert "no_headlines_in_range" in client.get("/llms.txt").text
    op = client.get("/openapi.json").json()["paths"]["/history/{symbol}"]["get"]
    assert "no_headlines_in_range" in op["responses"]["404"]["description"]
    assert "zero matching headlines" in op["description"]
