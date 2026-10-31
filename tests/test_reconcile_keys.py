"""app/maintenance/reconcile_keys.py against a fake, read-only Stripe."""

import re
import sqlite3
import types

import pytest

import tests.test_home  # noqa: F401
from app import alerts, billing
from app.billing import _db
from app.maintenance import reconcile_keys

PLAN_ITEMS = {"has_more": False, "data": [{"price": {"id": "price_starter"}}]}
OTHER_ITEMS = {"has_more": False, "data": [{"price": {"id": "price_listing_fix"}}]}


def _obj(value):
    return types.SimpleNamespace(to_dict=lambda: value)


def _missing(*args, **kwargs):
    raise billing.stripe.InvalidRequestError("No such subscription: sub_secret", "id", code="resource_missing")


@pytest.fixture
def world(monkeypatch, tmp_path):
    monkeypatch.setattr(billing, "DB_PATH", str(tmp_path / "r.db"))
    monkeypatch.setattr(billing.stripe, "api_key", "sk_live_dummy")
    billing.init_db()
    alerts.init_alerts_db()
    for tier in ("starter", "pro", "data"):
        monkeypatch.setenv(billing.TIERS[tier]["price_id_env"], f"price_{tier}")
    subs = {
        "sub_good": {"id": "sub_good", "status": "active", "items": PLAN_ITEMS},
        "sub_trial": {"id": "sub_trial", "status": "trialing", "items": PLAN_ITEMS},
        "sub_dead": {"id": "sub_dead", "status": "canceled", "items": PLAN_ITEMS},
        "sub_other": {"id": "sub_other", "status": "active", "items": OTHER_ITEMS},
    }
    customers = {"cus_oneoff": [], "cus_listing": [subs["sub_other"]], "cus_plan": [subs["sub_good"]]}

    def retrieve(sub_id):
        if sub_id == "sub_down":
            raise billing.stripe.APIConnectionError("sub_secret network down")
        if sub_id == "sub_badreq":
            raise billing.stripe.InvalidRequestError("nope sub_secret", "id", code="parameter_invalid")
        if sub_id not in subs:
            _missing()
        return _obj(subs[sub_id])

    def list_subs(customer, status, limit):
        assert status == "all"
        if customer not in customers:
            _missing()
        return _obj({"data": customers[customer]})

    monkeypatch.setattr(billing.stripe.Subscription, "retrieve", retrieve)
    monkeypatch.setattr(billing.stripe.Subscription, "list", list_subs)
    # Any writing Stripe call blows up.
    for name in ("create", "modify", "delete", "cancel"):
        monkeypatch.setattr(billing.stripe.Subscription, name, lambda *a, **k: pytest.fail("Stripe write"), raising=False)


def _key(key, tier="starter", status="active", sub=None, cus=None, email="person@secret-mail.dev"):
    with _db() as conn:
        conn.execute(
            "INSERT INTO api_keys (api_key, email, tier, stripe_customer_id, stripe_subscription_id, status, "
            "period_start, created_at) VALUES (?, ?, ?, ?, ?, ?, 'x', 'x')",
            (key, email, tier, cus, sub, status),
        )


def _watch(key, status="active"):
    with _db() as conn:
        conn.execute(
            "INSERT INTO watches (api_key, symbol, channel_type, channel_target, status, created_at) "
            "VALUES (?, 'BTC', 'webhook', 'https://webhook.site/secret-path', ?, 'x')",
            (key, status),
        )


def _statuses():
    with _db() as conn:
        return dict(conn.execute("SELECT api_key, status FROM api_keys").fetchall())


def _watch_statuses():
    with _db() as conn:
        return dict(conn.execute("SELECT api_key, status FROM watches").fetchall())


def _seed():
    _key("csk_aaaaGOODFULLKEY", sub="sub_good", cus="cus_plan")
    _key("csk_bbbbTRIALFULLKEY", sub="sub_trial")
    _key("csk_ccccDEADFULLKEY", sub="sub_dead", cus="cus_dead")
    _key("csk_ddddGONEFULLKEY", sub="sub_testmode")
    _key("csk_eeeeONEOFFFULLKEY", cus="cus_oneoff")
    _key("csk_ffffLISTINGFULLKEY", cus="cus_listing")
    _key("csk_ggggNOCUSTFULLKEY")
    _key("csk_hhhhOTHERSUBFULLKEY", sub="sub_other")
    _key("csk_iiiiDOWNFULLKEY", sub="sub_down")
    _key("csk_jjjjBADREQFULLKEY", sub="sub_badreq")
    _key("csk_kkkkDRIFTFULLKEY", status="canceled", sub="sub_good")
    _key("csk_llllOLDFULLKEY", status="canceled", sub="sub_dead")
    _key("csk_mmmmPLANCUSTFULLKEY", cus="cus_plan")
    _key("csk_freeFREEFULLKEY", tier="free")
    _watch("csk_aaaaGOODFULLKEY")
    _watch("csk_ccccDEADFULLKEY")
    _watch("csk_llllOLDFULLKEY")
    _watch("csk_key_row_is_gone")
    _watch("csk_iiiiDOWNFULLKEY")
    _watch("csk_freeFREEFULLKEY", status="canceled")


def _db_bytes():
    with open(billing.DB_PATH, "rb") as f:
        return f.read()


def test_dry_run_is_the_default_and_writes_nothing(world, capsys):
    _seed()
    before = _db_bytes()
    assert reconcile_keys.main([]) == 2  # 2: some keys couldn't be checked
    assert _db_bytes() == before
    out = capsys.readouterr().out
    assert "DRY RUN" in out and "Non-free keys checked: 13" in out
    assert "would be marked canceled: 6" in out
    assert "Active watches would be marked inactive: 3" in out


def test_dry_run_connection_cannot_write(world):
    conn = reconcile_keys._connect(apply=False)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("UPDATE api_keys SET status = 'x'")
    conn.close()


def test_explicit_dry_run_flag(world, capsys):
    _key("csk_ccccDEADFULLKEY", sub="sub_dead")
    assert reconcile_keys.main(["--dry-run"]) == 0
    assert _statuses()["csk_ccccDEADFULLKEY"] == "active"


def test_dry_run_and_apply_together_refused(world):
    with pytest.raises(SystemExit):
        reconcile_keys.main(["--dry-run", "--apply"])


def test_output_only_has_six_char_prefixes(world, capsys):
    _seed()
    reconcile_keys.main([])
    out = capsys.readouterr().out
    prefixes = set(re.findall(r"csk_\S*", out))
    assert prefixes and all(len(p) == 6 for p in prefixes)
    for secret in ("FULLKEY", "secret-mail", "cus_", "sub_", "webhook.site", "secret-path"):
        assert secret not in out
    assert "csk_cc" in out


def test_apply_cancels_only_keys_without_active_plan(world, capsys):
    _seed()
    assert reconcile_keys.main(["--apply"]) == 2
    assert _statuses() == {
        "csk_aaaaGOODFULLKEY": "active",
        "csk_bbbbTRIALFULLKEY": "active",
        "csk_ccccDEADFULLKEY": "canceled",
        "csk_ddddGONEFULLKEY": "canceled",  # test-mode subscription under a live key
        "csk_eeeeONEOFFFULLKEY": "canceled",  # the $5 top-up
        "csk_ffffLISTINGFULLKEY": "canceled",  # customer's only sub is another product
        "csk_ggggNOCUSTFULLKEY": "canceled",
        "csk_hhhhOTHERSUBFULLKEY": "canceled",
        "csk_iiiiDOWNFULLKEY": "active",  # Stripe didn't answer
        "csk_jjjjBADREQFULLKEY": "active",  # any other error isn't "missing"
        "csk_kkkkDRIFTFULLKEY": "canceled",  # reported, never revived
        "csk_llllOLDFULLKEY": "canceled",
        "csk_mmmmPLANCUSTFULLKEY": "active",  # no sub ID, but the customer has a plan
        "csk_freeFREEFULLKEY": "active",
    }
    assert _watch_statuses() == {
        "csk_aaaaGOODFULLKEY": "active",
        "csk_ccccDEADFULLKEY": "inactive",
        "csk_llllOLDFULLKEY": "inactive",
        "csk_key_row_is_gone": "inactive",
        "csk_iiiiDOWNFULLKEY": "active",
        "csk_freeFREEFULLKEY": "canceled",
    }
    out = capsys.readouterr().out
    assert "marked canceled: 6" in out and "would be" not in out


def test_apply_twice_changes_nothing_more(world, capsys):
    _seed()
    reconcile_keys.main(["--apply"])
    first = (_statuses(), _watch_statuses())
    capsys.readouterr()
    reconcile_keys.main(["--apply"])
    assert (_statuses(), _watch_statuses()) == first
    assert "marked canceled: 0" in capsys.readouterr().out


def test_drift_is_reported_not_changed(world, capsys):
    _key("csk_kkkkDRIFTFULLKEY", status="canceled", sub="sub_good")
    assert reconcile_keys.main(["--apply"]) == 0
    assert _statuses()["csk_kkkkDRIFTFULLKEY"] == "canceled"
    assert "active in Stripe (not changed, look at these): 1" in capsys.readouterr().out


def test_stripe_error_text_never_printed(world, capsys):
    _seed()
    reconcile_keys.main([])
    assert "sub_secret" not in capsys.readouterr().out


@pytest.mark.parametrize("flags", [[], ["--apply"]])
def test_missing_price_env_stops_it(world, monkeypatch, capsys, flags):
    """Otherwise every subscriber on that plan looks like a non-subscriber."""
    _key("csk_aaaaGOODFULLKEY", sub="sub_good")
    monkeypatch.delenv("STRIPE_PRICE_ID_PRO")
    assert reconcile_keys.main(flags) == 1
    assert _statuses()["csk_aaaaGOODFULLKEY"] == "active"
    assert "STRIPE_PRICE_ID_PRO" in capsys.readouterr().out


def test_missing_stripe_key_stops_it(world, monkeypatch):
    monkeypatch.setattr(billing.stripe, "api_key", "")
    assert reconcile_keys.main([]) == 1


def test_test_key_cannot_apply(world, monkeypatch, capsys):
    _key("csk_ddddGONEFULLKEY", sub="sub_livesub")
    monkeypatch.setattr(billing.stripe, "api_key", "sk_test_dummy")
    assert reconcile_keys.main(["--apply"]) == 1
    assert _statuses()["csk_ddddGONEFULLKEY"] == "active"
    assert reconcile_keys.main([]) == 0
    assert "TEST" in capsys.readouterr().out


def test_missing_database_is_not_created(world, monkeypatch, tmp_path):
    path = tmp_path / "typo.db"
    monkeypatch.setattr(billing, "DB_PATH", str(path))
    assert reconcile_keys.main(["--apply"]) == 1
    assert not path.exists()


def test_database_without_watches_table(world, capsys):
    with _db() as conn:
        conn.execute("DROP TABLE watches")
    _key("csk_ccccDEADFULLKEY", sub="sub_dead")
    assert reconcile_keys.main(["--apply"]) == 0
    assert _statuses()["csk_ccccDEADFULLKEY"] == "canceled"
