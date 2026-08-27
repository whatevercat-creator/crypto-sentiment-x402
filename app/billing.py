"""
Stripe-backed subscription tiers, layered on top of the x402 pay-per-call
API for buyers who'd rather have a predictable monthly bill (and don't want
to hold crypto) than pay per call in USDC.

Two ways to hit the sentiment data:
  - GET /sentiment/{symbol}     -- unchanged, x402 pay-per-call (agents)
  - GET /v1/sentiment/{symbol}  -- X-API-Key header, subscription quota

Tiers (see TIERS below for the source of truth):
  free     100 calls/mo,   $0    -- POST /billing/signup-free
  starter  3,000 calls/mo, $15/mo -- POST /billing/checkout/starter
  pro      15,000 calls/mo,$59/mo -- POST /billing/checkout/pro

Env vars (see .env.example / BILLING.md):
  STRIPE_SECRET_KEY        - from your Stripe dashboard (test or live)
  STRIPE_WEBHOOK_SECRET    - signing secret for the /billing/webhook endpoint
  STRIPE_PRICE_ID_STARTER  - Stripe Price ID for the Starter product
  STRIPE_PRICE_ID_PRO      - Stripe Price ID for the Pro product
  APP_BASE_URL             - public base URL of this deployment, used to
                              build Stripe checkout redirect URLs
  BILLING_DB_PATH          - sqlite file path (default "billing.db"; on
                              Render this is set to a path on the mounted
                              persistent disk so it survives redeploys --
                              see render.yaml)

NOTE ON STORAGE: this uses a single sqlite file, which is fine at MVP scale
on a single instance. If you outgrow it or move to multiple instances,
swap the sqlite3 calls below for a real Postgres connection -- the schema
is trivial to port.
"""

import os
import secrets
import sqlite3
import time
from datetime import datetime, timezone

import stripe
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
APP_BASE_URL = os.environ.get("APP_BASE_URL", "http://localhost:8000").rstrip("/")
DB_PATH = os.environ.get("BILLING_DB_PATH", "billing.db")

TIERS = {
    "free": {
        "label": "Free",
        "limit": 100,
        "price_usd": 0,
        "price_id_env": None,
        "alert_limit": 0,
    },
    "starter": {
        "label": "Starter",
        "limit": 3000,
        "price_usd": 15,
        "price_id_env": "STRIPE_PRICE_ID_STARTER",
        "alert_limit": 3,
    },
    "pro": {
        "label": "Pro",
        "limit": 15000,
        "price_usd": 59,
        "price_id_env": "STRIPE_PRICE_ID_PRO",
        "alert_limit": 15,
    },
}

router = APIRouter(prefix="/billing", tags=["billing"])


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS api_keys (
                api_key TEXT PRIMARY KEY,
                email TEXT,
                tier TEXT NOT NULL,
                stripe_customer_id TEXT,
                stripe_subscription_id TEXT,
                status TEXT NOT NULL DEFAULT 'active',
                period_calls_used INTEGER NOT NULL DEFAULT 0,
                period_start TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_api_keys_customer "
            "ON api_keys (stripe_customer_id)"
        )


def _new_key() -> str:
    return "csk_" + secrets.token_urlsafe(24)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class FreeSignup(BaseModel):
    email: str


@router.get("/pricing")
def pricing():
    return {
        "pay_per_call": {
            "protocol": "x402",
            "price_usd": 0.01,
            "endpoint": "GET /sentiment/{symbol}",
            "note": "No signup. Agents pay per call in USDC on Base.",
        },
        "subscriptions": {
            tier: {
                "label": cfg["label"],
                "price_usd_per_month": cfg["price_usd"],
                "calls_per_month": cfg["limit"],
                "endpoint": "GET /v1/sentiment/{symbol} (X-API-Key header)",
            }
            for tier, cfg in TIERS.items()
        },
        "signup": {
            "free": "POST /billing/signup-free {\"email\": \"...\"}",
            "starter": "POST /billing/checkout/starter",
            "pro": "POST /billing/checkout/pro",
        },
    }


@router.post("/signup-free")
def signup_free(body: FreeSignup):
    key = _new_key()
    now = _now_iso()
    with _db() as conn:
        conn.execute(
            "INSERT INTO api_keys "
            "(api_key, email, tier, status, period_calls_used, period_start, created_at) "
            "VALUES (?, ?, 'free', 'active', 0, ?, ?)",
            (key, body.email, now, now),
        )
    return {
        "api_key": key,
        "tier": "free",
        "calls_per_month": TIERS["free"]["limit"],
        "note": "Save this key now -- it will not be shown again. "
        "Use it as the X-API-Key header on GET /v1/sentiment/{symbol}.",
    }


@router.post("/checkout/{tier}")
def create_checkout(tier: str):
    if tier not in ("starter", "pro"):
        raise HTTPException(400, "tier must be 'starter' or 'pro' (use /billing/signup-free for the free tier)")

    price_id = os.environ.get(TIERS[tier]["price_id_env"], "")
    if not stripe.api_key or not price_id:
        raise HTTPException(
            503,
            f"Stripe isn't configured yet on this deployment -- set "
            f"STRIPE_SECRET_KEY and {TIERS[tier]['price_id_env']}. See BILLING.md.",
        )

    session = stripe.checkout.Session.create(
        mode="subscription",
        line_items=[{"price": price_id, "quantity": 1}],
        success_url=f"{APP_BASE_URL}/billing/success?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{APP_BASE_URL}/billing/cancel",
        metadata={"tier": tier},
    )
    return {"checkout_url": session.url}


@router.get("/success")
def checkout_success(session_id: str):
    if not stripe.api_key:
        raise HTTPException(503, "Stripe isn't configured on this deployment.")

    # Newer stripe-python (v9+) resource objects aren't dict-like anymore --
    # .to_dict() converts recursively so the .get() chains below still work.
    session = stripe.checkout.Session.retrieve(session_id).to_dict()
    customer_id = session.get("customer")

    # The webhook that provisions the key can land a moment after the
    # browser redirect does -- poll briefly rather than erroring right away.
    for _ in range(10):
        with _db() as conn:
            row = conn.execute(
                "SELECT api_key, tier FROM api_keys WHERE stripe_customer_id = ? "
                "ORDER BY created_at DESC LIMIT 1",
                (customer_id,),
            ).fetchone()
        if row:
            return {
                "api_key": row["api_key"],
                "tier": row["tier"],
                "note": "Save this key now -- it will not be shown again. "
                "Use it as the X-API-Key header on GET /v1/sentiment/{symbol}.",
            }
        time.sleep(1)

    raise HTTPException(
        202,
        "Payment received, your key is still being provisioned -- reload this "
        "page in a few seconds.",
    )


@router.get("/cancel")
def checkout_cancel():
    return {"status": "checkout canceled, no charge made"}


@router.post("/webhook")
async def stripe_webhook(request: Request):
    if not WEBHOOK_SECRET:
        raise HTTPException(503, "STRIPE_WEBHOOK_SECRET is not configured on this deployment.")

    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, WEBHOOK_SECRET)
    except (ValueError, stripe.error.SignatureVerificationError) as e:
        raise HTTPException(400, f"Invalid webhook payload/signature: {e}")

    event_type = event["type"]
    # Same story as checkout_success above -- convert to a plain dict so the
    # .get() calls below work regardless of stripe-python's object shape.
    data = event["data"]["object"].to_dict()

    if event_type == "checkout.session.completed":
        tier = (data.get("metadata") or {}).get("tier", "starter")
        customer_id = data.get("customer")
        subscription_id = data.get("subscription")
        email = (data.get("customer_details") or {}).get("email")
        key = _new_key()
        now = _now_iso()
        with _db() as conn:
            conn.execute(
                "INSERT INTO api_keys "
                "(api_key, email, tier, stripe_customer_id, stripe_subscription_id, "
                "status, period_calls_used, period_start, created_at) "
                "VALUES (?, ?, ?, ?, ?, 'active', 0, ?, ?)",
                (key, email, tier, customer_id, subscription_id, now, now),
            )

    elif event_type == "customer.subscription.deleted":
        customer_id = data.get("customer")
        with _db() as conn:
            conn.execute(
                "UPDATE api_keys SET status = 'canceled' WHERE stripe_customer_id = ?",
                (customer_id,),
            )

    elif event_type == "customer.subscription.updated":
        customer_id = data.get("customer")
        status = "active" if data.get("status") == "active" else "canceled"
        with _db() as conn:
            conn.execute(
                "UPDATE api_keys SET status = ? WHERE stripe_customer_id = ?",
                (status, customer_id),
            )

    return {"received": True}


def verify_and_charge_api_key(api_key: str) -> dict:
    """
    Validate an API key, reset its usage counter on a new calendar month,
    enforce the tier's quota, and (if allowed) record one call against it.
    Raises HTTPException on any failure. Returns usage info on success.
    """
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM api_keys WHERE api_key = ?", (api_key,)
        ).fetchone()

        if not row:
            raise HTTPException(
                401,
                "Invalid API key. Get one at POST /billing/signup-free "
                "or POST /billing/checkout/{tier}.",
            )
        if row["status"] != "active":
            raise HTTPException(403, "This subscription is not active.")
        if row["tier"] not in TIERS:
            raise HTTPException(500, "Unknown tier on this key -- contact support.")

        period_start = datetime.fromisoformat(row["period_start"])
        now = datetime.now(timezone.utc)
        calls_used = row["period_calls_used"]

        if (now.year, now.month) != (period_start.year, period_start.month):
            calls_used = 0
            conn.execute(
                "UPDATE api_keys SET period_calls_used = 0, period_start = ? "
                "WHERE api_key = ?",
                (_now_iso(), api_key),
            )

        limit = TIERS[row["tier"]]["limit"]
        if calls_used >= limit:
            raise HTTPException(
                429,
                f"Monthly quota exceeded ({limit} calls on the '{row['tier']}' tier). "
                f"Upgrade with POST /billing/checkout/{{tier}}, or it resets next "
                f"calendar month.",
            )

        conn.execute(
            "UPDATE api_keys SET period_calls_used = period_calls_used + 1 "
            "WHERE api_key = ?",
            (api_key,),
        )

        return {"tier": row["tier"], "calls_used": calls_used + 1, "limit": limit}


def get_key_info(api_key: str) -> dict:
    """
    Look up an API key WITHOUT charging a sentiment-call against its quota.
    Used by /alerts/* endpoints, which manage watches rather than fetch data.
    Raises HTTPException if the key is missing/inactive.
    """
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM api_keys WHERE api_key = ?", (api_key,)
        ).fetchone()
        if not row:
            raise HTTPException(
                401,
                "Invalid API key. Get one at POST /billing/signup-free "
                "or POST /billing/checkout/{tier}.",
            )
        if row["status"] != "active":
            raise HTTPException(403, "This subscription is not active.")
        if row["tier"] not in TIERS:
            raise HTTPException(500, "Unknown tier on this key -- contact support.")

        return {"tier": row["tier"], "email": row["email"]}
