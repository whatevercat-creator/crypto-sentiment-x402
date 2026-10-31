"""POST /billing/signup-free: sample and invalid emails, and the per-IP cap."""

import pytest
from fastapi.testclient import TestClient

import tests.test_home  # noqa: F401
from app import billing, main
from app.billing import _db

REAL = "someone@mail.test-shop.dev"


@pytest.fixture
def api(monkeypatch, tmp_path):
    monkeypatch.setattr(billing, "DB_PATH", str(tmp_path / "s.db"))
    billing.init_db()
    billing._signup_hits.clear()
    yield TestClient(main.app)
    billing._signup_hits.clear()


def _key_count():
    with _db() as conn:
        return conn.execute("SELECT COUNT(*) FROM api_keys").fetchone()[0]


@pytest.mark.parametrize("email", [
    "you@example.com", "YOU@EXAMPLE.COM", "a@example.org", "a@example.net",
    "a@mail.example.com", " a@example.com ",
])
def test_sample_domains_refused(api, email):
    r = api.post("/billing/signup-free", json={"email": email})
    assert r.status_code == 400 and "sample address" in r.json()["detail"]
    html = api.post("/billing/signup-free", data={"email": email})
    assert html.status_code == 400 and html.headers["content-type"].startswith("text/html")
    assert "csk_" not in r.text + html.text
    assert _key_count() == 0


@pytest.mark.parametrize("email", [
    "", "   ", "nobody", "a@", "@b.dev", "a b@c.dev", "a@b", "a@-b.dev", "a@localhost",
])
def test_invalid_emails_refused(api, email):
    assert api.post("/billing/signup-free", json={"email": email}).status_code == 400
    assert api.post("/billing/signup-free", data={"email": email}).status_code == 400
    assert _key_count() == 0


def test_lookalike_domain_allowed(api):
    assert api.post("/billing/signup-free", json={"email": "a@notexample.com"}).status_code == 200


def test_real_email_stored_trimmed(api):
    assert api.post("/billing/signup-free", json={"email": f"  {REAL} "}).status_code == 200
    with _db() as conn:
        assert conn.execute("SELECT email FROM api_keys").fetchone()[0] == REAL


def test_rate_limit_per_ip(api):
    alice = {"cf-connecting-ip": "203.0.113.1"}
    for _ in range(billing.SIGNUP_LIMIT):
        assert api.post("/billing/signup-free", json={"email": REAL}, headers=alice).status_code == 200
    blocked = api.post("/billing/signup-free", json={"email": REAL}, headers=alice)
    assert blocked.status_code == 429 and 0 < int(blocked.headers["retry-after"]) <= 3600
    form = api.post("/billing/signup-free", data={"email": REAL}, headers=alice)
    assert form.status_code == 429 and form.headers["content-type"].startswith("text/html")
    assert form.headers["retry-after"]
    assert _key_count() == billing.SIGNUP_LIMIT
    # Someone else isn't affected.
    bob = {"cf-connecting-ip": "203.0.113.2"}
    assert api.post("/billing/signup-free", json={"email": REAL}, headers=bob).status_code == 200


def test_forwarded_for_cannot_dodge_the_limit(api):
    for i in range(billing.SIGNUP_LIMIT):
        api.post("/billing/signup-free", json={"email": REAL},
                 headers={"cf-connecting-ip": "203.0.113.1", "x-forwarded-for": f"198.51.100.{i}"})
    r = api.post("/billing/signup-free", json={"email": REAL},
                 headers={"cf-connecting-ip": "203.0.113.1", "x-forwarded-for": "198.51.100.99"})
    assert r.status_code == 429


def test_refused_emails_do_not_use_up_the_limit(api):
    for _ in range(billing.SIGNUP_LIMIT + 2):
        api.post("/billing/signup-free", json={"email": "a@example.com"})
    assert api.post("/billing/signup-free", json={"email": REAL}).status_code == 200


def test_window_expires_and_memory_is_pruned(api, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(billing.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(billing, "_SIGNUP_TRACKED_IPS_MAX", 1)
    for _ in range(billing.SIGNUP_LIMIT):
        api.post("/billing/signup-free", json={"email": REAL}, headers={"cf-connecting-ip": "203.0.113.1"})
    clock[0] += billing.SIGNUP_WINDOW_SECONDS + 1
    r = api.post("/billing/signup-free", json={"email": REAL}, headers={"cf-connecting-ip": "203.0.113.2"})
    assert r.status_code == 200 and list(billing._signup_hits) == ["203.0.113.2"]
    r = api.post("/billing/signup-free", json={"email": REAL}, headers={"cf-connecting-ip": "203.0.113.1"})
    assert r.status_code == 200
