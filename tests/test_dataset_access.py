"""
Who can export the dataset: decided by each tier's dataset_access flag in
app/billing.py TIERS, not by the tier's name.
"""

import secrets

import pytest

from tests.test_home import client  # noqa: F401  (shared fixture)
from app.billing import TIERS, _db, _now_iso


def _key_for(tier: str) -> str:
    key = "csk_test_" + secrets.token_hex(8)
    now = _now_iso()
    with _db() as conn:
        conn.execute(
            "INSERT INTO api_keys (api_key, email, tier, status, period_calls_used, "
            "period_start, created_at) VALUES (?, ?, ?, 'active', 0, ?, ?)",
            (key, f"{tier}@example.com", tier, now, now),
        )
    return key


@pytest.mark.parametrize("tier", ["pro", "data"])
def test_dataset_tiers_can_export(client, tier):  # noqa: F811
    r = client.get("/dataset/export?format=json", headers={"X-API-Key": _key_for(tier)})
    assert r.status_code == 200
    assert "rows" in r.json()


@pytest.mark.parametrize("tier", ["starter", "free"])
def test_other_tiers_cannot_export(client, tier):  # noqa: F811
    r = client.get("/dataset/export", headers={"X-API-Key": _key_for(tier)})
    assert r.status_code == 403
    detail = r.json()["detail"]
    assert "POST /billing/checkout/pro" in detail
    assert "POST /billing/checkout/data" in detail


def test_access_follows_flag_not_name(client, monkeypatch):  # noqa: F811
    monkeypatch.setitem(TIERS["starter"], "dataset_access", True)
    monkeypatch.setitem(TIERS["data"], "dataset_access", False)
    assert client.get("/dataset/export", headers={"X-API-Key": _key_for("starter")}).status_code == 200
    assert client.get("/dataset/export", headers={"X-API-Key": _key_for("data")}).status_code == 403
