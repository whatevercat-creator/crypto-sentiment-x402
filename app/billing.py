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
  STRIPE_PRICE_ID_DATA     - Stripe Price ID for the Data Access product
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

import hashlib
import json
import logging
import os
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import parse_qs

import stripe
from email_validator import EmailNotValidError, validate_email
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel

from app.home import render_api_key_page, render_message_page

logger = logging.getLogger("app.billing")

stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
APP_BASE_URL = os.environ.get("APP_BASE_URL", "http://localhost:8000").rstrip("/")
DB_PATH = os.environ.get("BILLING_DB_PATH", "billing.db")
# x402 pay-per-call price, e.g. "$0.01". app/main.py charges this; the
# pricing endpoint and home page quote it.
X402_PRICE_USD = os.environ.get("X402_PRICE_USD", "$0.01")
# Stripe customer portal login (public, not a secret). Subscribers enter
# their checkout email and get a one-time code; there they can cancel (at
# period end), update their card and see invoices. See BILLING.md.
STRIPE_PORTAL_LOGIN_URL = "https://billing.stripe.com/p/login/dRm28qcbjeNJaWn86f1Nu00"

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


# The Stripe subscription statuses that keep a key working. Anything else
# (past_due, unpaid, paused, canceled, ...) turns the key off until Stripe
# says otherwise.
ACTIVE_SUBSCRIPTION_STATUSES = {"active", "trialing"}


def plan_tiers_by_price() -> dict:
    """{Stripe price ID: tier} for this service's plans. The same Stripe
    account sells other products, so this is what tells them apart."""
    plans = {}
    for tier, cfg in TIERS.items():
        price_id = os.environ.get(cfg["price_id_env"], "") if cfg["price_id_env"] else ""
        if price_id:
            plans[price_id] = tier
    return plans


def missing_price_envs() -> list:
    """Paid plans' price env vars that aren't set: their buyers can't be
    recognised until they are."""
    return [
        cfg["price_id_env"] for cfg in TIERS.values()
        if cfg["price_id_env"] and not os.environ.get(cfg["price_id_env"])
    ]


def tier_for_prices(price_ids: list) -> Optional[str]:
    """The tier these prices buy, or None unless every one is a plan price
    and together they name exactly one tier. Never guesses."""
    plans = plan_tiers_by_price()
    tiers = {plans.get(price_id) for price_id in price_ids}
    return tiers.pop() if len(tiers) == 1 else None


def tier_for_items(items: Optional[dict]) -> Optional[str]:
    """tier_for_prices for a Stripe list of line items or subscription items."""
    items = items or {}
    if items.get("has_more"):
        return None
    return tier_for_prices([(i.get("price") or {}).get("id") for i in items.get("data") or []])


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


# ---------------------------------------------------------------------------
# Key storage. API keys are stored as "sha256:<hex>" of the key, in the same
# api_keys.api_key / watches.api_key columns (joins and primary key unchanged).
# Keys are 24 random bytes, so a plain SHA-256 is enough: no salt or pepper.
# key_prefix keeps the first KEY_PREFIX_LEN characters (e.g. "csk_AbC1"), which
# isn't enough to use the key, for support lookups and script output.
#
# Transition: rows written before the migration (scripts/hash_existing_keys.py)
# still hold the plaintext key. Lookups accept either form until the migration
# has run; the stored value of the matched row (key_id) is what every later
# query uses, never the key the caller sent.
# ---------------------------------------------------------------------------
KEY_HASH_PREFIX = "sha256:"
KEY_PREFIX_LEN = 8

# Columns added after the first release. Nullable, added at startup when
# missing (ALTER TABLE ... ADD COLUMN doesn't rewrite existing rows).
#   key_prefix    first KEY_PREFIX_LEN characters of the key
#   last_used_at  set at issue; then on any request authenticated by an active
#                 key (including 429 over quota), at most once a day outside /v1
#   ended_at      paid: when the subscription ended; free: when switched off.
#                 Cleared when the key is active again. Purge clock: see
#                 scripts/purge_expired_keys.py.
_ADDED_COLUMNS = (("key_prefix", "TEXT"), ("last_used_at", "TEXT"), ("ended_at", "TEXT"))

# Stripe subscription statuses that mean the subscription has ended (as
# opposed to past_due/unpaid/paused, which can still recover).
ENDED_SUBSCRIPTION_STATUSES = {"canceled", "incomplete_expired"}


def hash_key(key: str) -> str:
    """How an API key is stored: "sha256:" + hex SHA-256 of the key."""
    return KEY_HASH_PREFIX + hashlib.sha256(key.encode("utf-8")).hexdigest()


def key_prefix(key: str) -> str:
    """The non-secret start of a key, kept for support and script output."""
    return key[:KEY_PREFIX_LEN]


def is_hashed(stored: Optional[str]) -> bool:
    return isinstance(stored, str) and stored.startswith(KEY_HASH_PREFIX)


def display_prefix(stored: str, prefix: Optional[str]) -> str:
    """What scripts print for a key: its key_prefix, or for a legacy
    plaintext row without one, the key's own first characters. Never any
    part of a hash."""
    if prefix:
        return prefix
    return "(no prefix)" if is_hashed(stored) else key_prefix(stored)


def _add_missing_columns(conn: sqlite3.Connection, table: str, columns) -> None:
    present = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    for name, sql_type in columns:
        if name not in present:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")


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
        _add_missing_columns(conn, "api_keys", _ADDED_COLUMNS)


def _new_key() -> str:
    return "csk_" + secrets.token_urlsafe(24)


# ---------------------------------------------------------------------------
# The 15-minute reveal cache for /billing/success. A paid key is shown for
# KEY_DISPLAY_MINUTES after checkout, but only its hash is stored, so the
# webhook keeps the plaintext here, in memory only (never on disk), keyed by
# the stored hash. One instance only, like the alert poller: a restart
# empties it, and the page then says the key was issued and to email for a
# replacement (the same path as after 15 minutes).
# ---------------------------------------------------------------------------
_reveal_cache: dict = {}
_reveal_lock = threading.Lock()


def _reveal_put(key_id: str, plaintext: str) -> None:
    expires = time.monotonic() + KEY_DISPLAY_MINUTES * 60
    with _reveal_lock:
        now = time.monotonic()
        for k in [k for k, (_, exp) in _reveal_cache.items() if exp <= now]:
            del _reveal_cache[k]
        _reveal_cache[key_id] = (plaintext, expires)


def _reveal_get(key_id: str) -> Optional[str]:
    with _reveal_lock:
        entry = _reveal_cache.get(key_id)
        if entry is None:
            return None
        plaintext, expires = entry
        if expires <= time.monotonic():
            del _reveal_cache[key_id]
            return None
        return plaintext


def _insert_new_key(conn, *, tier: str, email: Optional[str], customer_id=None,
                    subscription_id=None, now: str) -> tuple:
    """Insert a new key, stored hashed only. Returns (plaintext, key_id): the
    plaintext is for the one response that shows it, never stored."""
    key = _new_key()
    key_id = hash_key(key)
    conn.execute(
        "INSERT INTO api_keys "
        "(api_key, key_prefix, email, tier, stripe_customer_id, stripe_subscription_id, "
        "status, period_calls_used, period_start, created_at, last_used_at) "
        "VALUES (?, ?, ?, ?, ?, ?, 'active', 0, ?, ?, ?)",
        (key_id, key_prefix(key), email, tier, customer_id, subscription_id, now, now, now),
    )
    return key, key_id


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


# Bots copying the docs sign up with these; they're reserved, nobody's real
# address. Subdomains count too.
_SAMPLE_EMAIL_DOMAINS = ("example.com", "example.org", "example.net")

# Per-IP cap on free keys issued. In memory: one instance, and a restart
# forgetting it is harmless.
SIGNUP_LIMIT = 5
SIGNUP_WINDOW_SECONDS = 3600
_SIGNUP_TRACKED_IPS_MAX = 10_000
_signup_hits: dict = {}
_signup_lock = threading.Lock()


def _log_free_signup(email_domain: Optional[str], result: str, reason: Optional[str] = None) -> None:
    """One JSON line per POST /billing/signup-free (JSON or form). The email's
    domain only -- never the full address, never the key. email_domain is
    null when the address didn't parse, so raw input is never logged."""
    line = {
        "event": "free_signup",
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "email_domain": email_domain,
        "result": result,
    }
    if reason:
        line["reason"] = reason
    logger.info(json.dumps(line))


def _clean_signup_email(raw: str) -> tuple:
    """(address to store, its ASCII domain), or 400. Syntax only: no DNS
    lookup. Logs the free_signup line for a rejected address."""
    try:
        checked = validate_email(raw.strip(), check_deliverability=False)
    except EmailNotValidError:
        _log_free_signup(None, "rejected", "invalid_email")
        raise HTTPException(400, "Enter a valid email address.")
    domain = checked.ascii_domain.lower()
    if any(domain == d or domain.endswith("." + d) for d in _SAMPLE_EMAIL_DOMAINS):
        logger.info("[billing] free signup refused: sample email domain")
        _log_free_signup(domain, "rejected", "sample_domain")
        raise HTTPException(400, "That's a sample address from the docs. Use your own email address.")
    return checked.normalized, domain


def _check_free_signup_rate(request: Request, domain: str) -> None:
    try:
        _check_signup_rate(request)
    except HTTPException:
        _log_free_signup(domain, "rejected", "rate_limited")
        raise


def _client_ip(request: Request) -> str:
    # Live traffic comes through Cloudflare, which overwrites this header, so
    # a caller can't choose it. request.client isn't good enough on its own:
    # uvicorn fills it from X-Forwarded-For, which a caller can set.
    ip = request.headers.get("cf-connecting-ip")
    return ip or (request.client.host if request.client else "unknown")


def _check_signup_rate(request: Request) -> None:
    ip = _client_ip(request)
    now = time.monotonic()
    cutoff = now - SIGNUP_WINDOW_SECONDS
    with _signup_lock:
        recent = [t for t in _signup_hits.get(ip, []) if t > cutoff]
        if len(recent) >= SIGNUP_LIMIT:
            _signup_hits[ip] = recent
            raise HTTPException(
                429,
                "Too many free keys from this address. Try again later.",
                headers={"Retry-After": str(int(recent[0] - cutoff) + 1)},
            )
        recent.append(now)
        _signup_hits[ip] = recent
        if len(_signup_hits) > _SIGNUP_TRACKED_IPS_MAX:
            for tracked, hits in list(_signup_hits.items()):
                if hits[-1] <= cutoff:
                    del _signup_hits[tracked]


def _issue_free_key(email: str) -> dict:
    with _db() as conn:
        key, _ = _insert_new_key(conn, tier="free", email=email, now=_now_iso())
    return {
        "api_key": key,
        "tier": "free",
        "calls_per_month": TIERS["free"]["limit"],
        "note": "Save this key now -- it will not be shown again. "
        "Use it as the X-API-Key header on GET /v1/sentiment/{symbol}.",
    }


@router.post("/signup-free", openapi_extra={"security": []})
def signup_free(body: FreeSignup, request: Request):
    email, domain = _clean_signup_email(body.email)
    _check_free_signup_rate(request, domain)
    result = _issue_free_key(email)
    _log_free_signup(domain, "created")
    return result


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
    try:
        email, domain = _clean_signup_email((await _form_fields(request)).get("email", ""))
        _check_free_signup_rate(request, domain)
    except HTTPException as e:
        title = "Too many requests" if e.status_code == 429 else "Enter your email"
        return HTMLResponse(render_message_page(title, str(e.detail)), status_code=e.status_code, headers=e.headers)
    result = _issue_free_key(email)
    _log_free_signup(domain, "created")
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


@router.get("/success", include_in_schema=False)
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
            portal_url=STRIPE_PORTAL_LOGIN_URL,
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
            stored = row["api_key"]
            # Hashed rows: the plaintext only lives in the in-memory reveal
            # cache. Gone (a restart within the 15 minutes): same answer as
            # after 15 minutes. A legacy plaintext row (issued just before the
            # hashing deploy) is still shown as it was.
            api_key = _reveal_get(stored) if is_hashed(stored) else stored
            if api_key is None:
                raise HTTPException(410, _KEY_ALREADY_ISSUED)
            return {
                "api_key": api_key,
                "tier": row["tier"],
                "note": _KEY_SHOWN_NOTE,
            }
        time.sleep(1)

    raise HTTPException(
        202,
        "Payment received, your key is still being provisioned -- reload this "
        "page in a few seconds.",
    )


@router.get("/cancel", include_in_schema=False)
def checkout_cancel():
    return {"status": "checkout canceled, no charge made"}


@router.post("/webhook", include_in_schema=False)
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

    # A failed Stripe lookup answers 503 so Stripe redelivers the event.
    try:
        if event_type == "checkout.session.completed":
            _provision_from_checkout(data)
        elif event_type == "customer.subscription.deleted":
            _sync_subscription(data.get("id"), deleted=True)
        elif event_type == "customer.subscription.updated":
            _sync_subscription(data.get("id"), deleted=False)
    except stripe.StripeError as e:
        logger.error("[billing] Stripe lookup failed for a %s event: %s", event_type, type(e).__name__)
        raise HTTPException(503, "Could not confirm this event with Stripe.")

    return {"received": True}


def _ignore_purchase(reason: str) -> None:
    """Not one of this service's plans: no key. Reason only, so no customer
    data reaches the logs."""
    logger.info("[billing] checkout ignored: %s", reason)
    missing = missing_price_envs()
    if missing:
        # Otherwise a real plan purchase is silently dropped here.
        logger.error("[billing] plan prices not configured: %s", ",".join(missing))


def _provision_from_checkout(event_session: dict) -> None:
    """Create or upgrade the key for a completed checkout of one of this
    service's plans. The price decides the tier: metadata can be set by
    anyone creating a checkout on this account, and a Stripe event can be
    redelivered late, so the session is fetched fresh."""
    mode = event_session.get("mode")
    if mode != "subscription":
        _ignore_purchase(f"mode={mode}")
        return

    # Webhook events don't carry line_items.
    session = stripe.checkout.Session.retrieve(
        event_session.get("id"), expand=["line_items", "subscription"]
    ).to_dict()

    tier = tier_for_items(session.get("line_items"))
    if tier is None:
        _ignore_purchase("not one of this service's plan prices")
        return
    if session.get("payment_status") not in ("paid", "no_payment_required"):
        _ignore_purchase(f"payment_status={session.get('payment_status')}")
        return
    subscription = session.get("subscription") or {}
    if subscription.get("status") not in ACTIVE_SUBSCRIPTION_STATUSES:
        _ignore_purchase(f"subscription status={subscription.get('status')}")
        return

    subscription_id = subscription.get("id")
    now = _now_iso()
    with _db() as conn:
        existing = conn.execute(
            "SELECT 1 FROM api_keys WHERE stripe_subscription_id = ?", (subscription_id,)
        ).fetchone()
        if existing:  # redelivered event: the key already exists
            conn.execute(
                "UPDATE api_keys SET tier = ?, status = 'active', ended_at = NULL "
                "WHERE stripe_subscription_id = ?",
                (tier, subscription_id),
            )
            return
        key, key_id = _insert_new_key(
            conn, tier=tier, email=(session.get("customer_details") or {}).get("email"),
            customer_id=session.get("customer"), subscription_id=subscription_id, now=now,
        )
        # In the cache before the row commits, so /billing/success never
        # finds the row without its key.
        _reveal_put(key_id, key)


def _sync_subscription(subscription_id: Optional[str], deleted: bool) -> None:
    """Make the keys on this subscription match Stripe. Matches on the
    subscription, not the customer: the customer may hold other products.
    Stripe doesn't guarantee event order, so an update re-reads the current
    subscription rather than trusting the event's copy."""
    with _db() as conn:
        known = conn.execute(
            "SELECT 1 FROM api_keys WHERE stripe_subscription_id = ?", (subscription_id,)
        ).fetchone()
    if not known:
        logger.info("[billing] subscription event ignored: no key on this subscription")
        return

    # ended: "set" (the subscription has ended: start the purge clock if it
    # isn't running), "clear" (active again) or "keep" (off but can still
    # recover, e.g. past_due: the clock doesn't start).
    status, tier, ended = "canceled", None, "set"
    if not deleted:
        current = stripe.Subscription.retrieve(subscription_id).to_dict()
        stripe_status = current.get("status")
        if stripe_status in ACTIVE_SUBSCRIPTION_STATUSES:
            status, ended = "active", "clear"
        elif stripe_status not in ENDED_SUBSCRIPTION_STATUSES:
            ended = "keep"
        tier = tier_for_items(current.get("items"))
        if tier is None:
            logger.warning("[billing] subscription moved to a non-plan price: tier left unchanged")

    ended_sql = {
        "set": "COALESCE(ended_at, ?)",
        "clear": "NULL",
        "keep": "ended_at",
    }[ended]
    params = (status, tier) + ((_now_iso(),) if ended == "set" else ()) + (subscription_id,)
    with _db() as conn:
        conn.execute(
            f"UPDATE api_keys SET status = ?, tier = COALESCE(?, tier), ended_at = {ended_sql} "
            "WHERE stripe_subscription_id = ?",
            params,
        )


# last_used_at outside /v1 is written at most this often per key: a day is
# plenty for a 12-month clock, and it keeps reads from turning into writes.
LAST_USED_RESOLUTION = timedelta(days=1)


def _find_key_row(conn, api_key: Optional[str]) -> Optional[sqlite3.Row]:
    """The api_keys row for a key the caller sent: stored hashed, or (until
    scripts/hash_existing_keys.py has run) as legacy plaintext. A value that
    is itself a stored hash is never accepted as a key, so a leaked hash
    can't be used to authenticate."""
    if not isinstance(api_key, str) or not api_key or is_hashed(api_key):
        return None
    return conn.execute(
        "SELECT * FROM api_keys WHERE api_key IN (?, ?)", (hash_key(api_key), api_key)
    ).fetchone()


def _authenticate(conn, api_key: str) -> sqlite3.Row:
    """The active row for this key, or 401/403/500. A key that gets this far
    counts as used: last_used_at is refreshed (at most once a day) and
    committed straight away, so a 429 raised after it still counts."""
    row = _find_key_row(conn, api_key)
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
    now = datetime.now(timezone.utc)
    conn.execute(
        "UPDATE api_keys SET last_used_at = ? "
        "WHERE api_key = ? AND (last_used_at IS NULL OR last_used_at < ?)",
        (now.isoformat(), row["api_key"], (now - LAST_USED_RESOLUTION).isoformat()),
    )
    conn.commit()
    return row


def _check_api_key(conn, api_key: str) -> tuple:
    """Validate an API key, reset its usage counter on a new calendar month
    and enforce the tier's quota. Raises HTTPException on any failure.
    Returns (tier, calls_used, limit, key_id)."""
    row = _authenticate(conn, api_key)
    key_id = row["api_key"]

    period_start = datetime.fromisoformat(row["period_start"])
    now = datetime.now(timezone.utc)
    calls_used = row["period_calls_used"]

    if (now.year, now.month) != (period_start.year, period_start.month):
        calls_used = 0
        conn.execute(
            "UPDATE api_keys SET period_calls_used = 0, period_start = ? "
            "WHERE api_key = ?",
            (_now_iso(), key_id),
        )

    limit = TIERS[row["tier"]]["limit"]
    if calls_used >= limit:
        raise HTTPException(
            429,
            f"Monthly quota exceeded ({limit} calls on the '{row['tier']}' tier). "
            f"Upgrade with POST /billing/checkout/{{tier}}, or it resets next "
            f"calendar month.",
        )
    return row["tier"], calls_used, limit, key_id


def verify_api_key(api_key: str) -> None:
    """The checks verify_and_charge_api_key makes (401, 403, 429), without
    recording a call. Run it before doing any work for the key."""
    with _db() as conn:
        _check_api_key(conn, api_key)


def verify_and_charge_api_key(api_key: str) -> dict:
    """
    Validate an API key, reset its usage counter on a new calendar month,
    enforce the tier's quota, and (if allowed) record one call against it.
    Raises HTTPException on any failure. Returns usage info on success.
    """
    with _db() as conn:
        tier, calls_used, limit, key_id = _check_api_key(conn, api_key)
        conn.execute(
            "UPDATE api_keys SET period_calls_used = period_calls_used + 1, last_used_at = ? "
            "WHERE api_key = ?",
            (_now_iso(), key_id),
        )
        return {"tier": tier, "calls_used": calls_used + 1, "limit": limit}


def get_key_info(api_key: str) -> dict:
    """
    Look up an API key WITHOUT charging a sentiment-call against its quota.
    Used by /alerts/*, /dataset and /integrations, which don't fetch a
    sentiment reading. Raises HTTPException if the key is missing/inactive.

    key_id is the key as stored (its hash, or a legacy plaintext row's key):
    use it, never the key the caller sent, in every later query (watches).
    """
    with _db() as conn:
        row = _authenticate(conn, api_key)
        return {"tier": row["tier"], "email": row["email"], "key_id": row["api_key"]}
