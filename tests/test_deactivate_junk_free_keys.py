"""scripts/deactivate_junk_free_keys.py against a temporary SQLite billing DB:
dry run changes nothing and prints only prefixes and domains; --apply
deactivates (never deletes) matched free keys and never touches paid tiers."""

import hashlib
import importlib.util
import sqlite3
from pathlib import Path

import pytest

import tests.test_home  # noqa: F401  (env vars app.billing needs)
from app import billing

SCRIPT = Path(__file__).parent.parent / "scripts" / "deactivate_junk_free_keys.py"
_spec = importlib.util.spec_from_file_location("deactivate_junk_free_keys", SCRIPT)
junk = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(junk)

# (email, tier, status, expected rule or None)
ROWS = [
    ("test@gmail.com", "free", "active", "test_local_part"),
    ("Test123+x@outlook.com", "free", "active", "test_local_part"),
    ("bot@example.com", "free", "active", "reserved_domain"),
    ("a@mail.example.org", "free", "active", "reserved_domain"),
    ("dev@shop.test", "free", "active", "reserved_domain"),
    ("x@mailinator.com", "free", "active", "disposable_domain"),
    ("y@yopmail.com", "free", "active", "disposable_domain"),
    ("", "free", "active", "invalid_email"),
    ("nobody", "free", "active", "invalid_email"),
    ("jane.doe@gmail.com", "free", "active", None),
    ("tester.jane@company.dev", "free", "active", None),   # "tester" only as a whole local part
    ("a@notexample.com", "free", "active", None),
    ("test@example.com", "free", "canceled", None),        # not active: left alone
    ("test@example.com", "pro", "active", None),           # paid: never touched
    ("x@mailinator.com", "starter", "active", None),       # paid: never touched
    ("bot@example.com", "data", "active", None),           # paid: never touched
]


def _key(i):
    return "csk_" + hashlib.sha256(str(i).encode()).hexdigest()[:32]


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "billing.db"
    monkeypatch.setattr(billing, "DB_PATH", str(path))
    billing.init_db()  # the real api_keys schema
    with sqlite3.connect(path) as conn:
        for i, (email, tier, status, _) in enumerate(ROWS):
            conn.execute(
                "INSERT INTO api_keys (api_key, email, tier, status, period_calls_used, period_start, created_at) "
                "VALUES (?, ?, ?, ?, 0, '2026-11-01T00:00:00Z', ?)",
                (_key(i), email, tier, status, f"2026-11-01T00:00:{i:02d}Z"),
            )
    return path


def _snapshot(path):
    with sqlite3.connect(path) as conn:
        return conn.execute("SELECT api_key, email, tier, status FROM api_keys ORDER BY api_key").fetchall()


def _expected_matches():
    return {_key(i): rule for i, (_, _, _, rule) in enumerate(ROWS) if rule}


def test_rules_match_expected():
    for email, tier, status, rule in ROWS:
        if tier == "free" and status == "active":
            assert junk.match_rule(email) == rule, email


def test_dry_run_changes_nothing_and_prints_only_prefixes(db, capsys):
    before = _snapshot(db)
    assert junk.main(["--db", str(db)]) == 0
    out = capsys.readouterr().out
    assert _snapshot(db) == before
    assert out.startswith("MODE: DRY RUN")
    for name, _ in junk.RULES:
        assert f"  - {name}:" in out  # rule list at the top
    assert f"Matched: {len(_expected_matches())}" in out
    for key, rule in _expected_matches().items():
        assert f"{key[:8]:<12} {rule:<18}" in out
    for i in range(len(ROWS)):
        assert _key(i)[:9] not in out  # never more than 8 characters of a key
    # Domains only: no full email address and no local part.
    for email, *_ in ROWS:
        if "@" in email:
            assert email not in out and email.split("@")[0] + "@" not in out
    assert "nothing was changed" in out


def test_apply_deactivates_only_matched_free_keys(db, capsys):
    before = {k: (e, t, s) for k, e, t, s in _snapshot(db)}
    assert junk.main(["--db", str(db), "--apply"]) == 0
    after = {k: (e, t, s) for k, e, t, s in _snapshot(db)}
    assert set(after) == set(before)  # nothing deleted
    matched = _expected_matches()
    for k, (email, tier, status) in after.items():
        if k in matched:
            assert (tier, status) == ("free", "deactivated"), email
        else:
            assert (email, tier, status) == before[k], email  # untouched, incl. every paid key
    assert f"deactivated {len(matched)} free key(s)" in capsys.readouterr().out

    # Idempotent: a second run finds nothing left to do.
    assert junk.main(["--db", str(db), "--apply"]) == 0
    assert "Matched: 0" in capsys.readouterr().out


def test_missing_db_is_not_created(tmp_path):
    path = tmp_path / "nope.db"
    with pytest.raises(SystemExit):
        junk.main(["--db", str(path)])
    assert not path.exists()
