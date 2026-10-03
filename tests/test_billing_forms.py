"""
Home page Subscribe / Get free key buttons: plain HTML forms posting to the
billing endpoints, which redirect browsers to Stripe Checkout or show the
free key on a page, while API clients keep getting the same JSON.
"""

import types
from datetime import datetime, timedelta, timezone

import pytest

from tests.test_home import BROWSER_ACCEPT, client  # noqa: F401  (shared fixture)
from app import billing
from app.billing import _db, pricing

CHECKOUT_URL = "https://checkout.stripe.com/c/pay/cs_test_123"
# What a browser sends when submitting a <form method="post"> with no fields:
# the form content type and an empty body.
BROWSER_FORM_POST = {
    "Accept": BROWSER_ACCEPT,
    "Content-Type": "application/x-www-form-urlencoded",
}


@pytest.fixture
def stripe_configured(monkeypatch):
    created = []

    def create(**kwargs):
        created.append(kwargs)
        return types.SimpleNamespace(url=CHECKOUT_URL)

    monkeypatch.setattr(billing.stripe, "api_key", "sk_test_dummy")
    monkeypatch.setattr(billing.stripe.checkout.Session, "create", create)
    for tier in ("starter", "pro", "data"):
        monkeypatch.setenv(billing.TIERS[tier]["price_id_env"], f"price_{tier}")
    return created


def test_home_has_a_form_per_plan_and_developer_lines(client):  # noqa: F811
    html = client.get("/", headers={"Accept": BROWSER_ACCEPT}).text
    for tier in ("starter", "pro", "data"):
        assert f'<form method="post" action="/billing/checkout/{tier}">' in html
    assert html.count(">Subscribe</button>") == 3
    assert '<form method="post" action="/billing/signup-free">' in html
    assert '<input type="email" name="email" required' in html
    assert ">Get free key</button>" in html
    for command in pricing()["signup"].values():
        escaped = command.replace('"', "&quot;")
        assert f'<p class="dev">For developers: <code>{escaped}</code></p>' in html
    assert "<script" not in html.lower()


@pytest.mark.parametrize("tier", ["starter", "pro", "data"])
def test_subscribe_form_redirects_to_stripe(client, stripe_configured, tier):  # noqa: F811
    r = client.post(
        f"/billing/checkout/{tier}", content=b"", headers=BROWSER_FORM_POST,
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == CHECKOUT_URL
    assert stripe_configured[-1]["metadata"] == {"tier": tier}
    assert stripe_configured[-1]["line_items"] == [{"price": f"price_{tier}", "quantity": 1}]


def test_checkout_json_unchanged(client, stripe_configured):  # noqa: F811
    r = client.post("/billing/checkout/pro")
    assert r.status_code == 200
    assert r.json() == {"checkout_url": CHECKOUT_URL}


def test_subscribe_form_without_stripe_shows_page(client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(billing.stripe, "api_key", "")
    r = client.post(
        "/billing/checkout/pro", content=b"", headers=BROWSER_FORM_POST, follow_redirects=False
    )
    assert r.status_code == 503
    assert r.headers["content-type"].startswith("text/html")
    assert "Stripe isn&#x27;t configured yet" in r.text
    # API clients still get the JSON error.
    assert client.post("/billing/checkout/pro").json()["detail"].startswith(
        "Stripe isn't configured yet"
    )


def test_free_form_shows_key_page(client):  # noqa: F811
    r = client.post("/billing/signup-free", data={"email": "form@example.com"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert r.headers["cache-control"] == "no-store"
    with _db() as conn:
        row = conn.execute(
            "SELECT api_key, tier FROM api_keys WHERE email = ? ORDER BY created_at DESC LIMIT 1",
            ("form@example.com",),
        ).fetchone()
    assert row["tier"] == "free"
    assert f"<code>{row['api_key']}</code>" in r.text
    assert "Save this key now." in r.text
    assert "<script" not in r.text.lower()


@pytest.mark.parametrize("email", ["", "   ", "not-an-email"])
def test_free_form_rejects_missing_email(client, email):  # noqa: F811
    r = client.post("/billing/signup-free", data={"email": email})
    assert r.status_code == 400
    assert r.headers["content-type"].startswith("text/html")
    assert "csk_" not in r.text


def test_free_signup_json_unchanged(client):  # noqa: F811
    r = client.post("/billing/signup-free", json={"email": "json@example.com"})
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"api_key", "tier", "calls_per_month", "note"}
    assert body["tier"] == "free" and body["api_key"].startswith("csk_")
    missing = client.post("/billing/signup-free", json={})
    assert missing.status_code == 422
    assert missing.json()["detail"][0]["loc"] == ["body", "email"]


@pytest.fixture
def stripe_session(client, monkeypatch):  # noqa: F811
    """A completed checkout whose webhook already provisioned a Pro key."""
    monkeypatch.setattr(billing.stripe, "api_key", "sk_test_dummy")
    monkeypatch.setattr(billing.time, "sleep", lambda seconds: None)

    def retrieve(session_id):
        if session_id == "cs_bad":
            raise billing.stripe.InvalidRequestError("No such checkout.session", "session")
        return types.SimpleNamespace(to_dict=lambda: {"customer": f"cus_{session_id}"})

    monkeypatch.setattr(billing.stripe.checkout.Session, "retrieve", retrieve)
    key = "csk_success_page_test"
    with _db() as conn:
        conn.execute("DELETE FROM api_keys WHERE api_key = ?", (key,))
        conn.execute(
            "INSERT INTO api_keys (api_key, email, tier, stripe_customer_id, status, "
            "period_calls_used, period_start, created_at) "
            "VALUES (?, 'buyer@example.com', 'pro', 'cus_cs_paid', 'active', 0, ?, ?)",
            (key, billing._now_iso(), billing._now_iso()),
        )
    return client, key


def _issued_minutes_ago(key, minutes):
    issued = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    with _db() as conn:
        conn.execute("UPDATE api_keys SET created_at = ? WHERE api_key = ?", (issued, key))


def _assert_success_headers(r):
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["referrer-policy"] == "no-referrer"


def test_success_page_for_browsers(stripe_session):
    client, key = stripe_session
    r = client.get("/billing/success", params={"session_id": "cs_paid"},
                   headers={"Accept": BROWSER_ACCEPT})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    _assert_success_headers(r)
    assert "shown for 15 minutes" in r.text
    assert "won&#x27;t be shown again" not in r.text
    pro = billing.TIERS["pro"]
    assert f"Your {pro['label']} API key" in r.text
    assert f"<code>{key}</code>" in r.text
    assert f"{pro['limit']:,} calls a month" in r.text
    assert f"X-API-Key: {key}" in r.text and "/v1/sentiment/BTC" in r.text
    assert "<script" not in r.text.lower()


def test_success_json(stripe_session):
    client, key = stripe_session
    r = client.get("/billing/success", params={"session_id": "cs_paid"})
    assert r.status_code == 200
    _assert_success_headers(r)
    assert r.json() == {
        "api_key": key,
        "tier": "pro",
        "note": "Save this key now -- it's shown for 15 minutes after checkout. "
        "Use it as the X-API-Key header on GET /v1/sentiment/{symbol}.",
    }


@pytest.mark.parametrize("minutes,shown", [(14, True), (16, False)])
def test_key_shown_only_within_15_minutes(stripe_session, minutes, shown):
    client, key = stripe_session
    _issued_minutes_ago(key, minutes)
    as_json = client.get("/billing/success", params={"session_id": "cs_paid"})
    as_html = client.get("/billing/success", params={"session_id": "cs_paid"},
                         headers={"Accept": BROWSER_ACCEPT})
    for r in (as_json, as_html):
        _assert_success_headers(r)
        assert (key in r.text) is shown
    if shown:
        assert as_json.status_code == as_html.status_code == 200
        return
    assert as_json.status_code == as_html.status_code == 410
    detail = as_json.json()["detail"]
    assert "already issued" in detail and "shown for 15 minutes" in detail
    assert "hi@forgealone.com" in detail and "hi@unlisted.sh" not in detail
    assert as_html.headers["content-type"].startswith("text/html")
    assert "Your key was already issued" in as_html.text
    assert "hi@forgealone.com" in as_html.text


@pytest.mark.parametrize(
    "session_id,status,title",
    [("cs_bad", 400, "That checkout link isn&#x27;t valid"), ("cs_pending", 202, "Almost there")],
)
def test_success_problems_show_a_page(stripe_session, session_id, status, title):
    client, _ = stripe_session
    r = client.get("/billing/success", params={"session_id": session_id},
                   headers={"Accept": BROWSER_ACCEPT})
    assert r.status_code == status
    assert r.headers["content-type"].startswith("text/html")
    _assert_success_headers(r)
    assert title in r.text
    assert "csk_" not in r.text
    # API clients get the same status, as JSON, with the same headers.
    j = client.get("/billing/success", params={"session_id": session_id})
    assert j.status_code == status and "detail" in j.json()
    _assert_success_headers(j)
