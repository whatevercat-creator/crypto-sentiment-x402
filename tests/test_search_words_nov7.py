"""
2026-11-07: Bazaar descriptions open with search words (what it is) before
the "Use when" sentence, and the HTML home page carries the Google Search
Console verification tag (and nothing else does).
"""

import base64
import json

import pytest

from tests.test_home import BROWSER_ACCEPT, client  # noqa: F401  (shared fixture)
from app import main

VERIFY_TAG = '<meta name="google-site-verification" content="GAISr1qAqb7bLxzqKXkf0iccPzmZhsn43YTF9dENuJ0" />'
VERIFY_TOKEN = "GAISr1qAqb7bLxzqKXkf0iccPzmZhsn43YTF9dENuJ0"


@pytest.mark.parametrize("route,lead", [
    ("GET /sentiment/:symbol", "Crypto news sentiment API for "),
    ("GET /history/:symbol", "Historical crypto sentiment API: "),
])
def test_description_leads_with_search_words(route, lead):
    d = main.routes[route].description
    assert d.startswith(lead)
    # Search words come before the "Use when" sentence.
    assert "sentiment API" in d[: d.index("Use when an agent needs")]
    assert len(d) <= 500 and "not charged" in d


def test_html_home_has_google_verification_in_head(client):  # noqa: F811
    html = client.get("/", headers={"Accept": BROWSER_ACCEPT}).text
    head = html.split("</head>", 1)[0]
    assert head.count(VERIFY_TAG) == 1
    assert html.count(VERIFY_TOKEN) == 1


def test_verification_only_on_html_home(client):  # noqa: F811
    assert VERIFY_TOKEN not in client.get("/").text  # JSON index
    assert VERIFY_TOKEN not in client.get("/archive", headers={"Accept": BROWSER_ACCEPT}).text
    for path in ("/sentiment/BTC", "/history/BTC"):
        r = client.get(path)
        assert r.status_code == 402
        assert VERIFY_TOKEN not in r.text
        assert VERIFY_TOKEN not in base64.b64decode(r.headers["payment-required"]).decode()
