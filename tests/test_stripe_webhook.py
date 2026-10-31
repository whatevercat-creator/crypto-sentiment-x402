"""
POST /billing/webhook with real signed events: only this service's plans get
a key, the price decides the tier, and keys follow the subscription.
"""

import hashlib
import hmac
import json
import logging
import time
import types

import pytest
from fastapi.testclient import TestClient

import tests.test_home  # noqa: F401  (sets env before app imports)
from app import billing, main
from app.billing import _db

SECRET = "whsec_test"


@pytest.fixture
def stripe_api(monkeypatch, tmp_path):
    """Temp database, the three plan prices, and a fake Stripe holding the
    sessions and subscriptions the webhook will look up."""
    monkeypatch.setattr(billing, "DB_PATH", str(tmp_path / "b.db"))
    billing.init_db()
    monkeypatch.setattr(billing, "WEBHOOK_SECRET", SECRET)
    for tier in ("starter", "pro", "data"):
        monkeypatch.setenv(billing.TIERS[tier]["price_id_env"], f"price_{tier}")
    fake = types.SimpleNamespace(sessions={}, subscriptions={}, session_calls=[])

    def retrieve_session(session_id, **kwargs):
        fake.session_calls.append((session_id, kwargs))
        return types.SimpleNamespace(to_dict=lambda: fake.sessions[session_id])

    def retrieve_subscription(subscription_id):
        return types.SimpleNamespace(to_dict=lambda: fake.subscriptions[subscription_id])

    monkeypatch.setattr(billing.stripe.checkout.Session, "retrieve", retrieve_session)
    monkeypatch.setattr(billing.stripe.Subscription, "retrieve", retrieve_subscription)
    return fake


def _send(event_type, obj):
    payload = json.dumps({
        "id": "evt_1", "object": "event", "type": event_type, "data": {"object": obj},
    })
    ts = int(time.time())
    sig = hmac.new(SECRET.encode(), f"{ts}.{payload}".encode(), hashlib.sha256).hexdigest()
    return TestClient(main.app, raise_server_exceptions=False).post(
        "/billing/webhook", content=payload, headers={"stripe-signature": f"t={ts},v1={sig}"}
    )


def _items(*prices):
    return {"object": "list", "has_more": False, "data": [{"price": {"id": p}} for p in prices]}


def _subscription(sub_id="sub_1", status="active", price="price_starter"):
    return {"object": "subscription", "id": sub_id, "status": status, "items": _items(price)}


def _checkout(fake, session_id="cs_1", prices=("price_starter",), metadata=None, **overrides):
    """A completed subscription checkout, as the event and as Stripe's copy."""
    fake.sessions[session_id] = {
        "id": session_id,
        "mode": "subscription",
        "customer": "cus_1",
        "customer_details": {"email": "buyer@mail.test-shop.dev"},
        "payment_status": "paid",
        "line_items": _items(*prices),
        "subscription": _subscription(),
        **overrides,
    }
    event = {"object": "checkout.session", "id": session_id, "mode": "subscription",
             "metadata": metadata or {}}
    return _send("checkout.session.completed", event)


def _keys():
    with _db() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM api_keys").fetchall()]


@pytest.mark.parametrize("tier", ["starter", "pro", "data"])
def test_plan_checkout_creates_key_for_its_tier(stripe_api, tier):
    assert _checkout(stripe_api, prices=(f"price_{tier}",)).status_code == 200
    keys = _keys()
    assert len(keys) == 1
    assert keys[0]["tier"] == tier and keys[0]["status"] == "active"
    assert keys[0]["stripe_subscription_id"] == "sub_1"
    assert keys[0]["stripe_customer_id"] == "cus_1"
    assert keys[0]["email"] == "buyer@mail.test-shop.dev"
    assert stripe_api.session_calls[0][1]["expand"] == ["line_items", "subscription"]


def test_one_off_payment_creates_no_key_and_is_logged(stripe_api, caplog):
    """The 2026-10-29 $5 top-up."""
    event = {"object": "checkout.session", "id": "cs_topup", "mode": "payment",
             "customer": "cus_9", "customer_details": {"email": "secret-buyer@mail.test-shop.dev"}}
    with caplog.at_level(logging.INFO, logger="app.billing"):
        r = _send("checkout.session.completed", event)
    assert r.status_code == 200 and r.json() == {"received": True}
    assert _keys() == []
    assert stripe_api.session_calls == []
    assert "checkout ignored: mode=payment" in caplog.text
    assert "secret-buyer" not in caplog.text and "cus_9" not in caplog.text


def test_other_product_subscription_creates_no_key(stripe_api, caplog):
    with caplog.at_level(logging.INFO, logger="app.billing"):
        r = _checkout(stripe_api, prices=("price_listing_fix",))
    assert r.status_code == 200 and _keys() == []
    assert "checkout ignored" in caplog.text


def test_metadata_cannot_buy_a_tier(stripe_api):
    _checkout(stripe_api, prices=("price_listing_fix",), metadata={"tier": "pro"})
    assert _keys() == []
    _checkout(stripe_api, prices=("price_starter",), metadata={"tier": "pro"})
    assert [k["tier"] for k in _keys()] == ["starter"]


@pytest.mark.parametrize("prices", [
    (),
    ("price_starter", "price_listing_fix"),
    ("price_starter", "price_pro"),
])
def test_mixed_or_empty_checkouts_create_no_key(stripe_api, prices):
    _checkout(stripe_api, prices=prices)
    assert _keys() == []


def test_truncated_line_items_create_no_key(stripe_api):
    items = {**_items("price_starter"), "has_more": True}
    _checkout(stripe_api, line_items=items)
    assert _keys() == []


def test_unpaid_or_dead_subscription_creates_no_key(stripe_api):
    _checkout(stripe_api, payment_status="unpaid")
    _checkout(stripe_api, subscription=_subscription(status="canceled"))
    assert _keys() == []


def test_unset_price_env_never_matches_and_is_logged(stripe_api, monkeypatch, caplog):
    monkeypatch.delenv("STRIPE_PRICE_ID_DATA")
    with caplog.at_level(logging.INFO, logger="app.billing"):
        _checkout(stripe_api, prices=("price_data",))
        _checkout(stripe_api, session_id="cs_none", prices=("",))
    assert _keys() == []
    assert "plan prices not configured: STRIPE_PRICE_ID_DATA" in caplog.text


def test_redelivered_event_does_not_mint_a_second_key(stripe_api):
    _checkout(stripe_api)
    _checkout(stripe_api)
    assert len(_keys()) == 1


def test_stripe_outage_gets_redelivered_and_creates_no_key(stripe_api, monkeypatch):
    def down(*args, **kwargs):
        raise billing.stripe.APIConnectionError("cus_secret down")

    monkeypatch.setattr(billing.stripe.checkout.Session, "retrieve", down)
    r = _send("checkout.session.completed", {"object": "checkout.session", "id": "cs_1", "mode": "subscription"})
    assert r.status_code == 503 and "cus_secret" not in r.text
    assert _keys() == []


def test_deleted_cancels_only_that_subscriptions_key(stripe_api):
    _checkout(stripe_api)
    with _db() as conn:  # same customer, a second subscription
        conn.execute(
            "INSERT INTO api_keys (api_key, tier, stripe_customer_id, stripe_subscription_id, status, "
            "period_start, created_at) VALUES ('csk_other', 'pro', 'cus_1', 'sub_2', 'active', 'x', 'x')"
        )
    _send("customer.subscription.deleted", _subscription(status="canceled"))
    assert {k["stripe_subscription_id"]: k["status"] for k in _keys()} == {
        "sub_1": "canceled", "sub_2": "active",
    }


def test_events_for_another_products_subscription_change_nothing(stripe_api):
    _checkout(stripe_api)
    other = {**_subscription(sub_id="sub_other", status="canceled"), "customer": "cus_1"}
    assert _send("customer.subscription.deleted", other).status_code == 200
    assert _send("customer.subscription.updated", other).status_code == 200
    assert [k["status"] for k in _keys()] == ["active"]


@pytest.mark.parametrize("status,expected", [
    ("active", "active"), ("trialing", "active"), ("past_due", "canceled"),
    ("unpaid", "canceled"), ("paused", "canceled"), ("incomplete_expired", "canceled"),
    ("canceled", "canceled"),
])
def test_updated_follows_stripe_status(stripe_api, status, expected):
    _checkout(stripe_api)
    stripe_api.subscriptions["sub_1"] = _subscription(status=status)
    _send("customer.subscription.updated", _subscription(status=status))
    assert [k["status"] for k in _keys()] == [expected]


def test_recovery_turns_the_key_back_on(stripe_api):
    _checkout(stripe_api)
    stripe_api.subscriptions["sub_1"] = _subscription(status="past_due")
    _send("customer.subscription.updated", _subscription(status="past_due"))
    stripe_api.subscriptions["sub_1"] = _subscription(status="active")
    _send("customer.subscription.updated", _subscription(status="active"))
    assert [k["status"] for k in _keys()] == ["active"]


def test_late_update_cannot_revive_a_canceled_subscription(stripe_api):
    """Stripe doesn't guarantee order: the update's own copy says active, but
    Stripe's current state wins."""
    _checkout(stripe_api)
    stripe_api.subscriptions["sub_1"] = _subscription(status="canceled")
    _send("customer.subscription.updated", _subscription(status="active"))
    assert [k["status"] for k in _keys()] == ["canceled"]


def test_plan_change_moves_the_tier(stripe_api):
    _checkout(stripe_api)
    stripe_api.subscriptions["sub_1"] = _subscription(price="price_pro")
    _send("customer.subscription.updated", _subscription(price="price_pro"))
    assert [k["tier"] for k in _keys()] == ["pro"]


def test_move_to_unknown_price_leaves_tier_alone(stripe_api):
    _checkout(stripe_api)
    stripe_api.subscriptions["sub_1"] = _subscription(price="price_mystery")
    _send("customer.subscription.updated", _subscription(price="price_mystery"))
    assert [k["tier"] for k in _keys()] == ["starter"]


def test_bad_signature_refused(stripe_api):
    r = TestClient(main.app).post("/billing/webhook", content="{}", headers={"stripe-signature": "t=1,v1=bad"})
    assert r.status_code == 400 and _keys() == []
