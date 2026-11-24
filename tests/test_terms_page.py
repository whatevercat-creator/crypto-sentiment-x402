"""
GET /terms: browser terms page built from the code's own prices and plans,
linked from the home page footer, and kept out of /openapi.json.

Run with: python -m pytest
"""

import os
import tempfile

# Dummy values: app.main refuses to import without them; nothing is paid.
os.environ.setdefault("PAY_TO_ADDRESS", "0x000000000000000000000000000000000000dEaD")
os.environ.setdefault("CDP_API_KEY_ID", "test")
os.environ.setdefault("CDP_API_KEY_SECRET", "test")
os.environ.setdefault("BILLING_DB_PATH", os.path.join(tempfile.mkdtemp(), "billing.db"))

import pytest
from fastapi.testclient import TestClient

from app import archive, main, market
from app.billing import STRIPE_PORTAL_LOGIN_URL, TIERS, X402_PRICE_USD, init_db
from app.dataset import init_dataset_db
from app.home import TERMS_LAST_UPDATED
from app.hourly import init_hourly_db

BROWSER_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
FOOTER_LINKS = (
    '<a href="/terms">Terms</a>',
    '<a href="https://forgealone.com/privacy">Privacy</a>',
    '<a href="https://forgealone.com/terms">Forge Alone terms</a>',
)


@pytest.fixture
def client():
    init_db()
    init_dataset_db()
    init_hourly_db()
    # No `with` block: startup would launch the background pollers.
    return TestClient(main.app)


def test_terms_is_html_with_title_and_date(client):
    r = client.get("/terms")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "<title>Crypto News Sentiment API - Terms</title>" in r.text
    assert TERMS_LAST_UPDATED == "November 24, 2026"
    assert f"Last updated: {TERMS_LAST_UPDATED}" in r.text
    assert "<script" not in r.text.lower()


def test_terms_prices_come_from_constants(client):
    html = client.get("/terms").text
    assert f"<code>/sentiment/{{symbol}}</code>: {X402_PRICE_USD} per call" in html
    assert f"<code>/history/{{symbol}}</code>: {archive.HISTORY_PRICE_USD} per call" in html
    assert f"<code>/movers</code>: {market.MOVERS_PRICE_USD} per call" in html
    for cfg in TIERS.values():
        row = (
            f"<tr><td>{cfg['label']}</td><td>{cfg['limit']:,}</td>"
            f"<td>{cfg['alert_limit'] or 'None'}</td>"
            f"<td>{'Yes' if cfg['dataset_access'] else 'No'}</td>"
            f"<td>${cfg['price_usd']:,}</td></tr>"
        )
        assert row in html


def test_terms_says_what_plan_calls_are_and_fair_use_limits(client):
    text = " ".join(client.get("/terms").text.split())  # collapse HTML line breaks
    assert "<th>API-key calls per month</th>" in text
    assert (
        "API-key calls are requests to <code>/v1/sentiment/{symbol}</code> with an "
        "<code>X-API-Key</code> header. Pay-per-call (x402) requests don't count against them."
    ) in text
    assert "try to get around payment, monthly quotas or signup limits" in text
    assert "rate limits" not in text


def test_terms_follows_changed_constants(monkeypatch):
    from app import home

    tiers = {"x": {"label": "Test", "limit": 1234, "alert_limit": 7, "dataset_access": True, "price_usd": 99}}
    html = home.render_terms(
        tiers=tiers, price_usd="$0.42", history_price_usd="$0.43",
        movers_price_usd="$0.44", portal_url="https://billing.example/p",
    )
    for s in ("$0.42", "$0.43", "$0.44", "<td>1,234</td>", "<td>7</td>", "<td>$99</td>",
              'href="https://billing.example/p"'):
        assert s in html


def test_terms_links(client):
    html = client.get("/terms").text
    assert f'href="{STRIPE_PORTAL_LOGIN_URL}"' in html
    assert 'href="https://forgealone.com/terms"' in html
    assert 'href="https://forgealone.com/privacy"' in html
    footer = html.split("<footer>", 1)[1]
    for link in FOOTER_LINKS:
        assert link in footer


def test_home_footer_has_legal_links(client):
    html = client.get("/", headers={"Accept": BROWSER_ACCEPT}).text
    footer = html.split("<footer>", 1)[1]
    for link in FOOTER_LINKS:
        assert link in footer


def test_terms_not_in_openapi(client):
    schema = client.get("/openapi.json").json()
    assert "/terms" not in schema["paths"]
    assert "/terms" not in client.get("/openapi.json").text
