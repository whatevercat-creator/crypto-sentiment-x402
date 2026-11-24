"""
scripts/hash_existing_keys.py against temporary databases with the app's
real schema: the dry run is read-only and prints counts and prefixes only;
--apply hashes keys and watches in one go, fills the timestamps, leaves no
plaintext in the file, and is safe to rerun; unsafe states stop it with
nothing changed.
"""

import hashlib
import importlib.util
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

import tests.test_home  # noqa: F401  (env vars app.billing needs)
from app import billing
from app.alerts import init_alerts_db

SCRIPT = Path(__file__).parent.parent / "scripts" / "hash_existing_keys.py"
_spec = importlib.util.spec_from_file_location("hash_existing_keys", SCRIPT)
mig = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mig)

NOW = datetime(2026, 12, 1, 15, 0, tzinfo=timezone.utc)
ACTIVE = "csk_activeLegacyKey_AAAAAAAAAAAAAAAAAAAA"
CANCELED = "csk_canceledLegacyKey_BBBBBBBBBBBBBBBBBB"
EMAIL = "private-buyer@mail.test-shop.dev"


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "billing.db"
    monkeypatch.setattr(billing, "DB_PATH", str(path))
    monkeypatch.delenv("BILLING_DB_PATH", raising=False)
    monkeypatch.setattr(mig, "_now", lambda: NOW)
    billing.init_db()
    init_alerts_db()
    with sqlite3.connect(path) as conn:
        for key, status in ((ACTIVE, "active"), (CANCELED, "canceled")):
            conn.execute(
                "INSERT INTO api_keys (api_key, email, tier, status, period_calls_used, period_start, created_at) "
                "VALUES (?, ?, 'pro', ?, 3, '2026-11-01T00:00:00+00:00', '2026-10-01T00:00:00+00:00')",
                (key, EMAIL, status),
            )
        conn.execute(
            "INSERT INTO watches (api_key, symbol, channel_type, channel_target, created_at) "
            "VALUES (?, 'BTC', 'webhook', 'https://hook.customer.dev/m', '2026-10-01')", (ACTIVE,),
        )
        conn.execute(  # a watch whose key no longer exists
            "INSERT INTO watches (api_key, symbol, channel_type, channel_target, created_at) "
            "VALUES ('csk_orphanWatchKey_CCCCCCCCCCCCC', 'ETH', 'webhook', 'https://hook.customer.dev/o', '2026-10-01')"
        )
    return path


def _digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest(), Path(path).stat().st_mtime_ns


def _rows(path, sql):
    with sqlite3.connect(path) as conn:
        return conn.execute(sql).fetchall()


def test_dry_run_is_read_only_and_prints_prefixes_only(db, capsys):
    before = _digest(db)
    assert mig.main(["--db", str(db)]) == 0
    assert _digest(db) == before
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "plaintext: 2; hashed: 0" in out
    assert "csk_acti" in out and "csk_canc" in out
    for secret in (ACTIVE, CANCELED, EMAIL, "sha256:", "orphanWatchKey"):
        assert secret not in out
    assert "not matching any key: 1" in out
    assert not Path(str(db) + "-journal").exists()


def test_apply_hashes_keys_and_watches_and_fills_timestamps(db, capsys):
    assert mig.main(["--db", str(db), "--apply"]) == 0
    keys = {r[0]: r[1:] for r in _rows(db, "SELECT api_key, key_prefix, status, last_used_at, ended_at, period_calls_used FROM api_keys")}
    assert set(keys) == {mig.hash_key(ACTIVE), mig.hash_key(CANCELED)}
    assert keys[mig.hash_key(ACTIVE)] == ("csk_acti", "active", NOW.isoformat(), None, 3)
    assert keys[mig.hash_key(CANCELED)] == ("csk_canc", "canceled", NOW.isoformat(), NOW.isoformat(), 3)
    watches = sorted(r[0] for r in _rows(db, "SELECT api_key FROM watches"))
    assert watches == sorted([mig.hash_key(ACTIVE), mig.hash_key("csk_orphanWatchKey_CCCCCCCCCCCCC")])
    # The app still authenticates the migrated key with the same plaintext.
    assert billing.get_key_info(ACTIVE)["key_id"] == mig.hash_key(ACTIVE)
    assert "APPLIED" in capsys.readouterr().out


def test_no_plaintext_left_in_the_file_after_apply(db):
    # The key is also the start of a longer value in the old pages, so look for it outright.
    assert ACTIVE.encode() in Path(db).read_bytes()
    mig.main(["--db", str(db), "--apply"])
    raw = Path(db).read_bytes()
    for key in (ACTIVE, CANCELED, "csk_orphanWatchKey_CCCCCCCCCCCCC"):
        assert key.encode() not in raw
    assert not Path(str(db) + "-journal").exists()


def test_rerun_finds_nothing_and_changes_nothing(db, capsys):
    mig.main(["--db", str(db), "--apply"])
    rows_after_first = _rows(db, "SELECT * FROM api_keys ORDER BY api_key") + _rows(db, "SELECT * FROM watches ORDER BY id")
    capsys.readouterr()
    assert mig.main(["--db", str(db)]) == 0
    assert "plaintext: 0; hashed: 2" in capsys.readouterr().out
    assert mig.main(["--db", str(db), "--apply"]) == 0
    assert _rows(db, "SELECT * FROM api_keys ORDER BY api_key") + _rows(db, "SELECT * FROM watches ORDER BY id") == rows_after_first


def test_existing_timestamps_are_kept(db):
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE api_keys SET last_used_at = '2026-11-20T00:00:00+00:00', "
                     "ended_at = '2026-11-21T00:00:00+00:00' WHERE api_key = ?", (CANCELED,))
    mig.main(["--db", str(db), "--apply"])
    row = _rows(db, f"SELECT last_used_at, ended_at FROM api_keys WHERE api_key = '{mig.hash_key(CANCELED)}'")[0]
    assert row == ("2026-11-20T00:00:00+00:00", "2026-11-21T00:00:00+00:00")


def test_a_watch_written_with_plaintext_after_its_key_was_hashed_is_caught(db):
    mig.main(["--db", str(db), "--apply"])
    with sqlite3.connect(db) as conn:  # the mid-migration race
        conn.execute(
            "INSERT INTO watches (api_key, symbol, channel_type, channel_target, created_at) "
            "VALUES (?, 'SOL', 'webhook', 'https://hook.customer.dev/r', '2026-12-01')", (ACTIVE,),
        )
    mig.main(["--db", str(db), "--apply"])
    assert _rows(db, f"SELECT COUNT(*) FROM watches WHERE api_key = '{mig.hash_key(ACTIVE)}'")[0][0] == 2
    assert ACTIVE.encode() not in Path(db).read_bytes()


def test_stops_when_the_columns_are_missing(tmp_path, capsys):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE api_keys (api_key TEXT PRIMARY KEY, tier TEXT, status TEXT)")
        conn.execute("INSERT INTO api_keys VALUES ('csk_oldSchemaKey_123456', 'free', 'active')")
    for argv in ([], ["--apply"]):
        before = _digest(path)
        assert mig.main(["--db", str(path), *argv]) == 1
        assert _digest(path) == before
        assert "Deploy the hashing release first" in capsys.readouterr().out


def test_stops_on_a_hash_collision(db, capsys):
    with sqlite3.connect(db) as conn:  # a row already stored as ACTIVE's hash
        conn.execute(
            "INSERT INTO api_keys (api_key, tier, status, period_start, created_at) "
            "VALUES (?, 'free', 'active', 'x', 'x')", (mig.hash_key(ACTIVE),),
        )
    before = _digest(db)
    assert mig.main(["--db", str(db), "--apply"]) == 1
    assert _digest(db) == before
    out = capsys.readouterr().out
    assert "already exists" in out and "Nothing changed" in out and ACTIVE not in out


def test_stops_when_the_database_is_missing(tmp_path, capsys):
    missing = tmp_path / "nope.db"
    assert mig.main(["--db", str(missing), "--apply"]) == 1
    assert not missing.exists()
    assert "database not found" in capsys.readouterr().out


def test_works_without_a_watches_table(tmp_path, monkeypatch, capsys):
    path = tmp_path / "keys-only.db"
    monkeypatch.setattr(billing, "DB_PATH", str(path))
    billing.init_db()
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO api_keys (api_key, tier, status, period_start, created_at) "
                     "VALUES ('csk_noWatchesKey_12345678', 'free', 'active', 'x', 'x')")
    assert mig.main(["--db", str(path), "--apply"]) == 0
    assert "watches: no table" in capsys.readouterr().out
    assert _rows(path, "SELECT api_key FROM api_keys")[0][0] == mig.hash_key("csk_noWatchesKey_12345678")
