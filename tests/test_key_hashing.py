"""
API keys stored hashed: new keys never reach the database in plaintext,
legacy plaintext rows keep working until scripts/hash_existing_keys.py has
run, every watches query uses the stored key (key_id) so watches survive the
migration, the success page shows a paid key from the in-memory reveal
cache, and last_used_at / ended_at follow the rules the purge relies on.
"""

import importlib.util
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from tests.test_home import BROWSER_ACCEPT  # noqa: F401  (sets env before app imports)
from tests.test_stripe_webhook import _checkout, _send, _subscription, stripe_api  # noqa: F401
from app import alerts, billing, integrations, main
from app.alerts import ACTIVE_WATCHES_SQL, init_alerts_db
from app.billing import _db, _now_iso, get_key_info, hash_key
from app.dataset import init_dataset_db
from app.hourly import init_hourly_db

ROOT = Path(__file__).parent.parent


def _load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


hash_script = _load_script("hash_existing_keys")


@pytest.fixture
def db(monkeypatch, tmp_path):
    path = tmp_path / "keys.db"
    monkeypatch.setattr(billing, "DB_PATH", str(path))
    monkeypatch.setattr(billing, "_signup_hits", {})
    billing.init_db()
    init_alerts_db()
    init_dataset_db()
    init_hourly_db()
    return path


@pytest.fixture
def api(db):
    return TestClient(main.app)


def _legacy_key(key="csk_legacyPlainKey_0123456789abcdef", tier="pro", status="active", email="legacy@mail.test-shop.dev"):
    """A row as written before hashing: plaintext key, no key_prefix or timestamps."""
    now = _now_iso()
    with _db() as conn:
        conn.execute(
            "INSERT INTO api_keys (api_key, email, tier, status, period_calls_used, period_start, created_at) "
            "VALUES (?, ?, ?, ?, 0, ?, ?)",
            (key, email, tier, status, now, now),
        )
    return key


def _hashed_key(tier="pro"):
    with _db() as conn:
        key, _ = billing._insert_new_key(conn, tier=tier, email=f"{tier}@mail.test-shop.dev", now=_now_iso())
    return key


def _row(key):
    with _db() as conn:
        return conn.execute(
            "SELECT * FROM api_keys WHERE api_key IN (?, ?)", (hash_key(key), key)
        ).fetchone()


def _set(key, **cols):
    sets = ", ".join(f"{c} = ?" for c in cols)
    with _db() as conn:
        conn.execute(
            f"UPDATE api_keys SET {sets} WHERE api_key IN (?, ?)",
            (*cols.values(), hash_key(key), key),
        )


def _assert_plaintext_nowhere(db_path, key):
    """The full key is in no value of any table, nor anywhere in the file."""
    conn = sqlite3.connect(db_path)
    try:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
        for table in tables:
            for row in conn.execute(f'SELECT * FROM "{table}"'):
                for value in row:
                    assert not (isinstance(value, str) and key in value), (table, row)
    finally:
        conn.close()
    assert key.encode() not in Path(db_path).read_bytes()


def _ago(**delta):
    return (datetime.now(timezone.utc) - timedelta(**delta)).isoformat()


# --- helpers -------------------------------------------------------------------

def test_hash_key_is_prefixed_sha256():
    h = hash_key("csk_abc")
    assert h.startswith("sha256:") and len(h) == len("sha256:") + 64
    assert h == hash_key("csk_abc") != hash_key("csk_abd")


def test_migration_script_hashes_exactly_like_the_app():
    for key in ("csk_abc", "csk_" + "x" * 32, "csk_ünïcode"):
        assert hash_script.hash_key(key) == billing.hash_key(key)


def test_display_prefix_never_shows_a_hash():
    assert billing.display_prefix("sha256:abcdef", "csk_AbC1") == "csk_AbC1"
    assert billing.display_prefix("csk_legacyKey123", None) == "csk_lega"
    assert billing.display_prefix("sha256:abcdef", None) == "(no prefix)"


# --- new keys are stored hashed only --------------------------------------------

def test_free_signup_stores_only_the_hash(api, db):
    r = api.post("/billing/signup-free", json={"email": "someone@mail.test-shop.dev"})
    assert r.status_code == 200
    key = r.json()["api_key"]
    row = _row(key)
    assert row["api_key"] == hash_key(key)
    assert row["key_prefix"] == key[:8]
    assert row["last_used_at"] == row["created_at"]  # issue starts the clock
    assert row["ended_at"] is None
    _assert_plaintext_nowhere(db, key)
    assert get_key_info(key)["key_id"] == hash_key(key)


def test_paid_checkout_stores_only_the_hash_and_success_shows_the_key(stripe_api, monkeypatch):  # noqa: F811
    monkeypatch.setattr(billing.stripe, "api_key", "sk_test_dummy")
    monkeypatch.setattr(billing.time, "sleep", lambda seconds: None)
    assert _checkout(stripe_api, prices=("price_pro",)).status_code == 200

    client = TestClient(main.app)
    first = client.get("/billing/success", params={"session_id": "cs_1"})
    assert first.status_code == 200
    key = first.json()["api_key"]
    assert key.startswith("csk_")
    # A reload within the 15 minutes shows it again, HTML too.
    assert client.get("/billing/success", params={"session_id": "cs_1"}).json()["api_key"] == key
    html = client.get("/billing/success", params={"session_id": "cs_1"}, headers={"Accept": BROWSER_ACCEPT})
    assert f"<code>{key}</code>" in html.text

    row = _row(key)
    assert row["api_key"] == hash_key(key) and row["tier"] == "pro"
    _assert_plaintext_nowhere(billing.DB_PATH, key)


def test_success_after_a_restart_says_already_issued(stripe_api, monkeypatch):  # noqa: F811
    monkeypatch.setattr(billing.stripe, "api_key", "sk_test_dummy")
    monkeypatch.setattr(billing.time, "sleep", lambda seconds: None)
    _checkout(stripe_api, prices=("price_pro",))
    monkeypatch.setattr(billing, "_reveal_cache", {})  # what a restart leaves
    r = TestClient(main.app).get("/billing/success", params={"session_id": "cs_1"})
    assert r.status_code == 410
    assert "already issued" in r.json()["detail"] and "hi@forgealone.com" in r.json()["detail"]


def test_success_after_15_minutes_is_410_even_with_the_key_cached(stripe_api, monkeypatch):  # noqa: F811
    monkeypatch.setattr(billing.stripe, "api_key", "sk_test_dummy")
    monkeypatch.setattr(billing.time, "sleep", lambda seconds: None)
    _checkout(stripe_api, prices=("price_pro",))
    with _db() as conn:
        conn.execute("UPDATE api_keys SET created_at = ?", (_ago(minutes=16),))
    r = TestClient(main.app).get("/billing/success", params={"session_id": "cs_1"})
    assert r.status_code == 410 and "api_key" not in r.text


def test_redelivered_checkout_makes_no_second_key(stripe_api, monkeypatch):  # noqa: F811
    monkeypatch.setattr(billing.stripe, "api_key", "sk_test_dummy")
    monkeypatch.setattr(billing.time, "sleep", lambda seconds: None)
    _checkout(stripe_api, prices=("price_pro",))
    key = TestClient(main.app).get("/billing/success", params={"session_id": "cs_1"}).json()["api_key"]
    _checkout(stripe_api, prices=("price_pro",))
    with _db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM api_keys").fetchone()[0] == 1
    assert TestClient(main.app).get("/billing/success", params={"session_id": "cs_1"}).json()["api_key"] == key


def test_reveal_cache_entries_expire(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(billing.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(billing, "_reveal_cache", {})
    billing._reveal_put("sha256:x", "csk_secret")
    assert billing._reveal_get("sha256:x") == "csk_secret"
    clock[0] += billing.KEY_DISPLAY_MINUTES * 60 + 1
    assert billing._reveal_get("sha256:x") is None
    assert billing._reveal_cache == {}


# --- lookup: hashed and legacy keys, nothing else ----------------------------------

@pytest.fixture
def stubs(monkeypatch):
    async def payload(symbol):
        return {"symbol": symbol, "overall_sentiment": {"label": "bullish", "average_compound": 0.4}}

    sent = []

    async def post(url, body):
        sent.append((str(url), body))
        return True, ""

    monkeypatch.setattr(main, "compute_sentiment_payload", payload)
    monkeypatch.setattr(integrations, "compute_sentiment_payload", payload)
    monkeypatch.setattr(alerts, "post_json", post)
    return sent


WATCH = {"symbol": "BTC", "channel_type": "webhook", "channel_target": "https://hook.customer.dev/k"}


@pytest.mark.parametrize("kind", ["hashed", "legacy"])
def test_key_works_everywhere(api, stubs, kind):
    key = _hashed_key("pro") if kind == "hashed" else _legacy_key(tier="pro")
    h = {"X-API-Key": key}
    assert api.get("/v1/sentiment/BTC", headers=h).status_code == 200
    created = api.post("/alerts/watch", headers=h, json=WATCH)
    assert created.status_code == 200
    assert [w["id"] for w in api.get("/alerts/watch", headers=h).json()["watches"]] == [created.json()["id"]]
    assert api.get("/alerts/history", headers=h).status_code == 200
    assert api.get("/dataset/export?format=json", headers=h).status_code == 200
    body = json.dumps({"api_key": key, "symbol": "BTC"})
    assert len(api.post("/integrations/tradingview", content=body).json()["relayed_to"]) == 1
    assert len(api.post(f"/integrations/tradingview/{key}", content='{"symbol": "BTC"}').json()["relayed_to"]) == 1
    assert api.delete(f"/alerts/watch/{created.json()['id']}", headers=h).status_code == 200


def test_a_stored_hash_is_not_a_key(api, stubs):
    key = _hashed_key("pro")
    stolen = hash_key(key)
    assert api.get("/v1/sentiment/BTC", headers={"X-API-Key": stolen}).status_code == 401
    with pytest.raises(HTTPException) as e:
        get_key_info(stolen)
    assert e.value.status_code == 401


def test_unknown_key_401_and_inactive_403(api, stubs):
    assert api.get("/v1/sentiment/BTC", headers={"X-API-Key": "csk_nope"}).status_code == 401
    key = _legacy_key(status="canceled")
    assert api.get("/v1/sentiment/BTC", headers={"X-API-Key": key}).status_code == 403


def test_new_watch_is_stored_under_the_hash(api, stubs, db):
    key = _hashed_key("pro")
    api.post("/alerts/watch", headers={"X-API-Key": key}, json=WATCH)
    with _db() as conn:
        assert [r[0] for r in conn.execute("SELECT api_key FROM watches")] == [hash_key(key)]
    _assert_plaintext_nowhere(db, key)


# --- the regression: watches by key_id, through the migration ----------------------

def test_watches_follow_the_key_through_the_migration(api, stubs, db, capsys):
    key = _legacy_key(tier="pro")
    h = {"X-API-Key": key}
    watch_id = api.post("/alerts/watch", headers=h, json=WATCH).json()["id"]
    with _db() as conn:
        conn.execute(
            "INSERT INTO alert_events (watch_id, symbol, new_label, delivered, sent_at) "
            "VALUES (?, 'BTC', 'bullish', 1, ?)", (watch_id, _now_iso()),
        )

    assert hash_script.main(["--db", str(db), "--apply"]) == 0
    capsys.readouterr()

    # Stored hashed now: a query by the raw key would find nothing...
    with _db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM watches WHERE api_key = ?", (key,)).fetchone()[0] == 0
        assert conn.execute(f"SELECT COUNT(*) FROM {ACTIVE_WATCHES_SQL}").fetchone()[0] == 1  # poller still joins
    # ...but every route finds the watch, because they query by key_id.
    assert [w["id"] for w in api.get("/alerts/watch", headers=h).json()["watches"]] == [watch_id]
    assert len(api.get("/alerts/history", headers=h).json()["events"]) == 1
    body = json.dumps({"api_key": key, "symbol": "BTC"})
    assert api.post("/integrations/tradingview", content=body).json()["relayed_to"][0]["watch_id"] == watch_id
    path_form = api.post(f"/integrations/tradingview/{key}", content='{"symbol": "BTC"}').json()
    assert path_form["relayed_to"][0]["watch_id"] == watch_id
    # The tier's watch limit still counts the migrated watch.
    assert api.post("/alerts/watch", headers=h, json=WATCH).status_code == 200
    with _db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM watches WHERE api_key = ?", (hash_key(key),)).fetchone()[0] == 2
    assert api.delete(f"/alerts/watch/{watch_id}", headers=h).status_code == 200
    _assert_plaintext_nowhere(db, key)


# --- last_used_at ------------------------------------------------------------------

def test_v1_call_updates_last_used(api, stubs):
    key = _hashed_key()
    _set(key, last_used_at=_ago(days=40))
    assert api.get("/v1/sentiment/BTC", headers={"X-API-Key": key}).status_code == 200
    assert _row(key)["last_used_at"] > _ago(minutes=1)


def test_other_routes_update_last_used_at_most_daily(api, stubs):
    key = _hashed_key()
    _set(key, last_used_at=_ago(days=2))
    api.get("/alerts/watch", headers={"X-API-Key": key})
    assert _row(key)["last_used_at"] > _ago(minutes=1)
    recent = _ago(hours=3)
    _set(key, last_used_at=recent)
    api.get("/alerts/watch", headers={"X-API-Key": key})
    assert _row(key)["last_used_at"] == recent  # no write within the day


def test_a_429_counts_as_use(api, stubs):
    key = _hashed_key("free")
    _set(key, period_calls_used=billing.TIERS["free"]["limit"], last_used_at=_ago(days=100))
    assert api.get("/v1/sentiment/BTC", headers={"X-API-Key": key}).status_code == 429
    assert _row(key)["last_used_at"] > _ago(minutes=1)


def test_an_inactive_key_is_not_use(api, stubs):
    key = _legacy_key(status="canceled")
    old = _ago(days=100)
    _set(key, last_used_at=old)
    assert api.get("/v1/sentiment/BTC", headers={"X-API-Key": key}).status_code == 403
    assert _row(key)["last_used_at"] == old


# --- ended_at follows the subscription -------------------------------------------

def _ended(sub_id="sub_1"):
    with _db() as conn:
        return conn.execute(
            "SELECT status, ended_at FROM api_keys WHERE stripe_subscription_id = ?", (sub_id,)
        ).fetchone()


def test_ended_at_follows_the_subscription(stripe_api):  # noqa: F811
    _checkout(stripe_api, prices=("price_pro",))
    assert tuple(_ended()) == ("active", None)

    stripe_api.subscriptions["sub_1"] = _subscription(status="past_due", price="price_pro")
    _send("customer.subscription.updated", {"object": "subscription", "id": "sub_1"})
    assert tuple(_ended()) == ("canceled", None)  # off, but not ended: no clock

    stripe_api.subscriptions["sub_1"] = _subscription(status="active", price="price_pro")
    _send("customer.subscription.updated", {"object": "subscription", "id": "sub_1"})
    assert tuple(_ended()) == ("active", None)

    stripe_api.subscriptions["sub_1"] = _subscription(status="canceled", price="price_pro")
    _send("customer.subscription.updated", {"object": "subscription", "id": "sub_1"})
    status, first_end = _ended()
    assert status == "canceled" and first_end is not None

    _send("customer.subscription.deleted", {"object": "subscription", "id": "sub_1"})
    assert tuple(_ended()) == ("canceled", first_end)  # the first end stands


def test_deleted_subscription_sets_ended_and_recovery_clears_it(stripe_api):  # noqa: F811
    _checkout(stripe_api, prices=("price_pro",))
    _send("customer.subscription.deleted", {"object": "subscription", "id": "sub_1"})
    assert _ended()["ended_at"] is not None
    _checkout(stripe_api, prices=("price_pro",))  # a redelivered checkout turns it back on
    assert tuple(_ended()) == ("active", None)


# --- schema ----------------------------------------------------------------------

def test_init_db_adds_the_columns_to_an_old_database(monkeypatch, tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE api_keys (api_key TEXT PRIMARY KEY, email TEXT, tier TEXT NOT NULL, "
            "stripe_customer_id TEXT, stripe_subscription_id TEXT, status TEXT NOT NULL DEFAULT 'active', "
            "period_calls_used INTEGER NOT NULL DEFAULT 0, period_start TEXT NOT NULL, created_at TEXT NOT NULL)"
        )
        conn.execute("INSERT INTO api_keys (api_key, tier, period_start, created_at) VALUES ('csk_old', 'free', 'x', 'x')")
    monkeypatch.setattr(billing, "DB_PATH", str(path))
    billing.init_db()
    billing.init_db()  # idempotent
    with sqlite3.connect(path) as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(api_keys)")]
        row = conn.execute("SELECT api_key, key_prefix, last_used_at, ended_at FROM api_keys").fetchone()
    assert cols[-3:] == ["key_prefix", "last_used_at", "ended_at"]
    assert row == ("csk_old", None, None, None)  # existing rows untouched
