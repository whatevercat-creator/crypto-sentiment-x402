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
  data     3,000 calls/mo, $29/mo -- POST /billing/checkout/data
  (dataset export: every tier with dataset_access = True, i.e. pro + data)

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
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs

import stripe
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel

from app.home import render_api_key_page, render_message_page

stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
APP_BASE_URL = os.environ.get("APP_BASE_URL", "http://localhost:8000").rstrip("/")
DB_PATH = os.environ.get("BILLING_DB_PATH", "billing.db")
# x402 pay-per-call price, e.g. "$0.01". app/main.py charges this; the
# pricing endpoint and home page quote it.
X402_PRICE_USD = os.environ.get("X402_PRICE_USD", "$0.01")

TIERS = {
    "free": {
        "label": "Free",
        "limit": 100,
        "price_usd": 0,
        "price_id_env": None,
        "alert_limit": 0,
        "dataset_access": False,
    },
    "starter": {
        "label": "Starter",
        "limit": 3000,
        "price_usd": 15,
        "price_id_env": "STRIPE_PRICE_ID_STARTER",
        "alert_limit": 3,
        "dataset_access": False,
    },
    "pro": {
        "label": "Pro",
        "limit": 15000,
        "price_usd": 59,
        "price_id_env": "STRIPE_PRICE_ID_PRO",
        "alert_limit": 15,
        "dataset_access": True,
    },
    "data": {
        "label": "Data Access",
        "limit": 3000,
        "price_usd": 29,
        "price_id_env": "STRIPE_PRICE_ID_DATA",
        "alert_limit": 0,
        "dataset_access": True,
    },
}


def _is_form_post(request: Request) -> bool:
    """A plain HTML <form method="post"> (the home page's Subscribe / Get
    free key buttons) sends application/x-www-form-urlencoded. API clients
    send JSON or no body at all."""
    ctype = request.headers.get("content-type", "")
    return ctype.split(";")[0].strip().lower() == "application/x-www-form-urlencoded"


class _FormAwareRoute(APIRoute):
    """Sends HTML form posts to the path's entry in _FORM_HANDLERS, which
    answers with a page or a redirect. Every other request goes through
    FastAPI's normal handler untouched, so API clients keep exactly the
    JSON (and 422 validation errors) they always got."""

    def get_route_handler(self):
        default_handler = super().get_route_handler()

        async def handler(request: Request):
            form_handler = _FORM_HANDLERS.get(self.path_format)
            if form_handler is not None and _is_form_post(request):
                return await form_handler(request)
            return await default_handler(request)

        return handler


router = APIRouter(prefix="/billing", tags=["billing"], route_class=_FormAwareRoute)

# OpenAPI security for routes that need a subscription key in X-API-Key.
# Declaring it tells x402 directories (x402scan) these routes take an API
# key rather than an x402 payment, so they aren't probed for a 402. The
# scheme itself is added to components.securitySchemes in app/main.py.
API_KEY_SCHEME = "ApiKeyAuth"
API_KEY_SECURITY = {"security": [{API_KEY_SCHEME: []}]}


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


def _plan_includes(cfg: dict) -> str:
    """One line describing what a tier grants, built from the tier config
    itself so it can't drift from what the code actually enforces."""
    parts = [f"{cfg['limit']:,} sentiment calls/month on /v1/sentiment"]
    if cfg["alert_limit"]:
        parts.append(f"up to {cfg['alert_limit']} sentiment-shift alert watches")
    else:
        parts.append("no alerts")
    if cfg["dataset_access"]:
        parts.append("full historical dataset export (/dataset/export)")
    else:
        parts.append("no dataset export")
    return ", ".join(parts)


def dataset_plans() -> dict:
    """The subscription plans (as /billing/pricing lists them) that unlock
    /dataset/export."""
    subs = pricing()["subscriptions"]
    return {tier: subs[tier] for tier, cfg in TIERS.items() if cfg["dataset_access"]}


class FreeSignup(BaseModel):
    email: str


@router.get("/pricing", openapi_extra={"security": []})
def pricing():
    return {
        "pay_per_call": {
            "protocol": "x402",
            "price_usd": float(X402_PRICE_USD.lstrip("$")),
            "endpoint": "GET /sentiment/{symbol}",
            "note": "No signup. Agents pay per call in USDC on Base.",
        },
        "subscriptions": {
            tier: {
                "label": cfg["label"],
                "price_usd_per_month": cfg["price_usd"],
                "calls_per_month": cfg["limit"],
                "includes": _plan_includes(cfg),
                "endpoint": "GET /v1/sentiment/{symbol} (X-API-Key header)",
            }
            for tier, cfg in TIERS.items()
        },
        "signup": {
            "free": "POST /billing/signup-free {\"email\": \"...\"}",
            "starter": "POST /billing/checkout/starter",
            "pro": "POST /billing/checkout/pro",
            "data": "POST /billing/checkout/data",
        },
    }


def _issue_free_key(email: str) -> dict:
    key = _new_key()
    now = _now_iso()
    with _db() as conn:
        conn.execute(
            "INSERT INTO api_keys "
            "(api_key, email, tier, status, period_calls_used, period_start, created_at) "
            "VALUES (?, ?, 'free', 'active', 0, ?, ?)",
            (key, email, now, now),
        )
    return {
        "api_key": key,
        "tier": "free",
        "calls_per_month": TIERS["free"]["limit"],
        "note": "Save this key now -- it will not be shown again. "
        "Use it as the X-API-Key header on GET /v1/sentiment/{symbol}.",
    }


@router.post("/signup-free", openapi_extra={"security": []})
def signup_free(body: FreeSignup):
    return _issue_free_key(body.email)


def _checkout_url(tier: str) -> str:
    if tier not in ("starter", "pro", "data"):
        raise HTTPException(400, "tier must be 'starter', 'pro', or 'data' (use /billing/signup-free for the free tier)")

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
    return session.url


@router.post("/checkout/{tier}", openapi_extra={"security": []})
def create_checkout(tier: str):
    return {"checkout_url": _checkout_url(tier)}


async def _form_fields(request: Request) -> dict:
    body = (await request.body()).decode("utf-8", errors="replace")
    return {k: v[0] for k, v in parse_qs(body).items()}


async def _signup_free_form(request: Request):
    email = (await _form_fields(request)).get("email", "").strip()
    if not email or "@" not in email:
        return HTMLResponse(
            render_message_page("Enter your email", "Go back and enter an email address to get a free key."),
            status_code=400,
        )
    result = _issue_free_key(email)
    return HTMLResponse(
        render_api_key_page(
            plan_label=TIERS["free"]["label"].lower(),
            api_key=result["api_key"],
            calls_per_month=result["calls_per_month"],
            base_url=APP_BASE_URL,
            save_note="It won't be shown again.",
        ),
        # The page shows a secret that's never shown again; keep it out of caches.
        headers={"Cache-Control": "no-store"},
    )


async def _checkout_form(request: Request):
    try:
        url = _checkout_url(request.path_params["tier"])
    except HTTPException as e:
        return HTMLResponse(
            render_message_page("Checkout isn't available", str(e.detail)),
            status_code=e.status_code,
        )
    return RedirectResponse(url, status_code=303)


_FORM_HANDLERS = {
    "/billing/signup-free": _signup_free_form,
    "/billing/checkout/{tier}": _checkout_form,
}


# /billing/success shows a subscriber's key only this long after the
# checkout completed (when the webhook stored the key), so a leaked or
# shared success link stops revealing it. Lost keys are reissued by hand:
# see "How to reissue a key" in BILLING.md.
KEY_DISPLAY_MINUTES = 15
SUPPORT_EMAIL = "hi@forgealone.com"
_KEY_SHOWN_NOTE = (
    f"Save this key now -- it's shown for {KEY_DISPLAY_MINUTES} minutes after checkout. "
    "Use it as the X-API-Key header on GET /v1/sentiment/{symbol}."
)
_KEY_ALREADY_ISSUED = (
    f"Your API key was already issued. It's shown for {KEY_DISPLAY_MINUTES} minutes "
    f"after checkout. If you've lost it, email {SUPPORT_EMAIL} from the address you "
    "used at checkout and we'll send you a replacement."
)
# On every /billing/success response: the key must not be cached, and the
# session_id in the URL must not leak to other sites via Referer.
_SUCCESS_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}

# Page titles for /billing/success failures, by status code.
_SUCCESS_ERROR_TITLES = {
    202: "Almost there",
    400: "That checkout link isn't valid",
    410: "Your key was already issued",
    503: "Checkout isn't available",
}


@router.get("/success", openapi_extra={"security": []})
def checkout_success(session_id: str, request: Request):
    # Stripe redirects the buyer's browser here after checkout. Browsers
    # (Accept: text/html, as on GET /) get a page; API clients get JSON.
    wants_html = "text/html" in request.headers.get("accept", "").lower()
    try:
        result = _provisioned_key(session_id)
    except HTTPException as e:
        if not wants_html:
            raise HTTPException(e.status_code, e.detail, headers=_SUCCESS_HEADERS)
        return HTMLResponse(
            render_message_page(_SUCCESS_ERROR_TITLES.get(e.status_code, "Something went wrong"), str(e.detail)),
            status_code=e.status_code,
            headers={**_SUCCESS_HEADERS, "Vary": "Accept"},
        )
    if not wants_html:
        return JSONResponse(result, headers=_SUCCESS_HEADERS)
    cfg = TIERS[result["tier"]]
    return HTMLResponse(
        render_api_key_page(
            plan_label=cfg["label"],
            api_key=result["api_key"],
            calls_per_month=cfg["limit"],
            base_url=APP_BASE_URL,
            save_note=f"It's shown for {KEY_DISPLAY_MINUTES} minutes after checkout. "
            "Keep it secret: anyone with it can use your plan's calls.",
        ),
        headers={**_SUCCESS_HEADERS, "Vary": "Accept"},
    )


def _issued_at(row: sqlite3.Row) -> datetime:
    issued = datetime.fromisoformat(row["created_at"])
    return issued if issued.tzinfo else issued.replace(tzinfo=timezone.utc)


def _provisioned_key(session_id: str) -> dict:
    if not stripe.api_key:
        raise HTTPException(503, "Stripe isn't configured on this deployment.")

    # Newer stripe-python (v9+) resource objects aren't dict-like anymore --
    # .to_dict() converts recursively so the .get() chains below still work.
    try:
        session = stripe.checkout.Session.retrieve(session_id).to_dict()
    except stripe.InvalidRequestError:
        # Unknown or malformed session_id -- the caller's fault, not ours.
        raise HTTPException(400, "Invalid or unknown checkout session_id.")
    customer_id = session.get("customer")

    # The webhook that provisions the key can land a moment after the
    # browser redirect does -- poll briefly rather than erroring right away.
    for _ in range(10):
        with _db() as conn:
            row = conn.execute(
                "SELECT api_key, tier, created_at FROM api_keys WHERE stripe_customer_id = ? "
                "ORDER BY created_at DESC LIMIT 1",
                (customer_id,),
            ).fetchone()
        if row:
            if datetime.now(timezone.utc) - _issued_at(row) > timedelta(minutes=KEY_DISPLAY_MINUTES):
                raise HTTPException(410, _KEY_ALREADY_ISSUED)
            return {
                "api_key": row["api_key"],
                "tier": row["tier"],
                "note": _KEY_SHOWN_NOTE,
            }
        time.sleep(1)

    raise HTTPException(
        202,
        "Payment received, your key is still being provisioned -- reload this "
        "page in a few seconds.",
    )


@router.get("/cancel", openapi_extra={"security": []})
def checkout_cancel():
    return {"status": "checkout canceled, no charge made"}


@router.post("/webhook", openapi_extra={"security": []})
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
