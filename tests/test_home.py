"""
GET / content negotiation: browsers get the HTML home page, everyone else
keeps the JSON index, and the page stays free with the x402 paywall live.

Run with: python -m pytest
"""

import os
import tempfile

# app.main refuses to import without these; dummy values are fine because
# the facilitator is stubbed below and nothing settles a real payment.
os.environ.setdefault("PAY_TO_ADDRESS", "0x000000000000000000000000000000000000dEaD")
os.environ.setdefault("CDP_API_KEY_ID", "test")
os.environ.setdefault("CDP_API_KEY_SECRET", "test")
os.environ.setdefault("BILLING_DB_PATH", os.path.join(tempfile.mkdtemp(), "billing.db"))

import pytest
from fastapi.testclient import TestClient
from x402.schemas import SupportedKind, SupportedResponse

from app import main
from app.billing import init_db, pricing
from app.dataset import init_dataset_db

BROWSER_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"

# What GET / returned before the HTML page existed -- agents depend on it.
ORIGINAL_ROOT_JSON = {
    "name": "Crypto Sentiment API",
    "protocol": "x402",
    "network": main.NETWORK_MODE,
    "price_per_call": main.PRICE_USD,
    "paid_endpoint": "/sentiment/{symbol}",
    "example": "/sentiment/BTC",
    "subscriptions": "/billing/pricing",
    "alerts": "/alerts/watch (requires an active Starter/Pro X-API-Key)",
    "dataset": "/dataset/info",
    "rapidapi": "/rapidapi/sentiment/{symbol} (RapidAPI-proxied traffic only)",
    "integrations": "/integrations/tradingview/{api_key} (relays TradingView alerts through your existing /alerts/watch channels)",
    "signal_validation": "/validation (does the score lead or lag price? published whatever it shows)",
    "docs": "/docs",
}


@pytest.fixture
def client(monkeypatch):
    # Stub the facilitator's /supported call so the x402 middleware can
    # initialize offline and return a real 402 for /sentiment/*.
    monkeypatch.setattr(
        main.facilitator,
        "get_supported",
        lambda: SupportedResponse(
            kinds=[SupportedKind(x402Version=2, scheme="exact", network=main.CAIP2_NETWORK)]
        ),
    )
    init_db()
    init_dataset_db()
    # No `with` block: startup would launch the background pollers, which
    # hit the network.
    return TestClient(main.app)


def test_browser_gets_html_with_name_and_price(client):
    r = client.get("/", headers={"Accept": BROWSER_ACCEPT})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "Crypto Sentiment API" in r.text
    assert main.PRICE_USD in r.text
    assert "<script" not in r.text.lower()


def test_html_page_reflects_pricing_and_validation(client):
    html = client.get("/", headers={"Accept": BROWSER_ACCEPT}).text
    for plan in pricing()["subscriptions"].values():
        assert plan["label"] in html
    assert "2026-10-12" in html  # first_results_expected from app/validation.json
    assert 'href="/validation"' in html


@pytest.mark.parametrize(
    "headers",
    [{}, {"Accept": "application/json"}, {"Accept": "*/*"}],
    ids=["no-accept", "json", "wildcard"],
)
def test_non_browser_gets_original_json(client, headers):
    r = client.get("/", headers=headers)
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/json"
    assert r.json() == ORIGINAL_ROOT_JSON


def test_home_page_free_while_paywall_active(client):
    # The paywall is live: a browser hitting the paid route gets a 402.
    paid = client.get("/sentiment/BTC", headers={"Accept": BROWSER_ACCEPT})
    assert paid.status_code == 402
    # ...but the home page, for browsers and agents alike, does not.
    for accept in (BROWSER_ACCEPT, "application/json"):
        r = client.get("/", headers={"Accept": accept})
        assert r.status_code == 200
        assert "payment-required" not in {k.lower() for k in r.headers}


def test_root_schema_unchanged(client):
    schema = client.get("/openapi.json").json()
    assert "text/html" not in str(schema["paths"]["/"])


def test_pricing_plans_have_includes(client):
    subs = client.get("/billing/pricing").json()["subscriptions"]
    assert all(plan["includes"] for plan in subs.values())
    assert subs["starter"]["includes"] != subs["data"]["includes"]
    assert "dataset export" in subs["data"]["includes"]


def test_dataset_info_has_price_and_plan(client):
    info = client.get("/dataset/info").json()
    subs = client.get("/billing/pricing").json()["subscriptions"]
    assert info["unlocked_by"] == [
        {
            "plan": tier,
            "label": subs[tier]["label"],
            "price_usd_per_month": subs[tier]["price_usd_per_month"],
            "includes": subs[tier]["includes"],
            "checkout": f"POST /billing/checkout/{tier}",
        }
        for tier in ("pro", "data")
    ]
    assert info["get_access"] == "POST /billing/checkout/pro or POST /billing/checkout/data"


def test_pro_shown_as_including_dataset_export(client):
    subs = client.get("/billing/pricing").json()["subscriptions"]
    assert "dataset export (/dataset/export)" in subs["pro"]["includes"]
    assert "no dataset export" in subs["starter"]["includes"]
    html = client.get("/", headers={"Accept": BROWSER_ACCEPT}).text
    assert "Included with Pro and Data Access" in html


@pytest.mark.parametrize("path", ["/", "/health"])
def test_head_returns_200(client, path):
    r = client.head(path)
    assert r.status_code == 200
    assert r.content == b""


def test_billing_success_unknown_session_is_400(client, monkeypatch):
    from app import billing

    def missing(session_id):
        raise billing.stripe.InvalidRequestError(
            f"No such checkout.session: '{session_id}'", "session"
        )

    monkeypatch.setattr(billing.stripe, "api_key", "sk_test_dummy")
    monkeypatch.setattr(billing.stripe.checkout.Session, "retrieve", missing)
    r = client.get("/billing/success", params={"session_id": "cs_test_nope"})
    assert r.status_code == 400
    assert r.json() == {"detail": "Invalid or unknown checkout session_id."}
