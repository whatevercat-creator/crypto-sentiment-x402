"""
app/reissue_key.py: the admin script for reissuing a lost API key from the
Render Shell. It rotates the key in place and moves alert watches with it.
"""

import os
import secrets
import subprocess
import sys

import pytest
from fastapi import HTTPException

from tests.test_home import client  # noqa: F401  (shared fixture: sets env, inits DBs)
from app import reissue_key
from app.alerts import init_alerts_db
from app.billing import _db, get_key_info

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _add_key(email, tier="pro", status="active", customer=None, used=7):
    key = "csk_" + secrets.token_urlsafe(24)
    customer = customer or "cus_" + secrets.token_hex(6)
    with _db() as conn:
        conn.execute(
            "INSERT INTO api_keys (api_key, email, tier, stripe_customer_id, stripe_subscription_id, "
            "status, period_calls_used, period_start, created_at) "
            "VALUES (?, ?, ?, ?, 'sub_x', ?, ?, '2026-10-01T00:00:00+00:00', '2026-10-01T00:00:00+00:00')",
            (key, email, tier, customer, status, used),
        )
    return key, customer


def _row(key):
    with _db() as conn:
        return conn.execute("SELECT * FROM api_keys WHERE api_key = ?", (key,)).fetchone()


@pytest.fixture
def email(client):  # noqa: F811
    init_alerts_db()
    return f"buyer-{secrets.token_hex(4)}@example.com"


def test_reissue_by_email_rotates_in_place(email, capsys):
    old, customer = _add_key(email)
    with _db() as conn:
        conn.execute(
            "INSERT INTO watches (api_key, symbol, channel_type, channel_target, created_at) "
            "VALUES (?, 'BTC', 'webhook', 'https://hooks.test/x', '2026-10-01')",
            (old,),
        )

    assert reissue_key.main(["--email", email.upper()], confirm=lambda prompt: "y") == 0

    out = capsys.readouterr().out
    new = out.split("New API key: ")[1].split()[0]
    assert new.startswith("csk_") and new != old
    assert old not in out  # only ever shown masked
    with pytest.raises(HTTPException) as e:
        get_key_info(old)
    assert e.value.status_code == 401
    assert get_key_info(new)["tier"] == "pro"
    row = _row(new)
    assert (row["stripe_customer_id"], row["stripe_subscription_id"]) == (customer, "sub_x")
    assert row["period_calls_used"] == 7
    assert row["created_at"] == "2026-10-01T00:00:00+00:00"  # success link stays expired
    with _db() as conn:
        assert conn.execute("SELECT api_key FROM watches WHERE api_key IN (?, ?)", (old, new)).fetchall()[0][0] == new


def test_reissue_by_customer_with_yes(email, capsys):
    old, customer = _add_key(email)
    assert reissue_key.main(["--customer", customer, "--yes"], confirm=None) == 0
    assert _row(old) is None
    assert f"Send it only to {email}" in capsys.readouterr().out


def test_declining_changes_nothing(email):
    old, _ = _add_key(email)
    assert reissue_key.main(["--email", email], confirm=lambda prompt: "n") == 1
    assert _row(old) is not None


def test_no_match_or_only_canceled(email, capsys):
    assert reissue_key.main(["--email", email, "--yes"]) == 1
    assert "No API key matches" in capsys.readouterr().out
    old, _ = _add_key(email, status="canceled")
    assert reissue_key.main(["--email", email, "--yes"]) == 1
    assert "None of these keys is active" in capsys.readouterr().out
    assert _row(old) is not None


def test_several_active_keys_need_a_prefix(email, capsys):
    first, _ = _add_key(email, tier="starter")
    second, _ = _add_key(email, tier="data")
    assert reissue_key.main(["--email", email, "--yes"]) == 1
    assert "--key-prefix" in capsys.readouterr().out
    assert reissue_key.main(["--email", email, "--key-prefix", second[:12], "--yes"]) == 0
    assert _row(first) is not None and _row(second) is None


def test_runs_as_a_module():
    # How it's invoked on Render: `python -m app.reissue_key ...` from /app.
    r = subprocess.run(
        [sys.executable, "-m", "app.reissue_key", "--help"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr
    assert "--email" in r.stdout and "--customer" in r.stdout
