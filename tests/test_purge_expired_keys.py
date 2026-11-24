"""
scripts/purge_expired_keys.py: each rule at its boundary (Feb 29 included),
free keys go at whichever rule comes first, paid keys only once their
subscription has ended, linked watches and alert events go with them, the
dry run is read-only, unsafe states stop it, and nothing deleted stays in
the file.
"""

import hashlib
import importlib.util
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import tests.test_home  # noqa: F401  (env vars app.billing needs)
from app import billing
from app.alerts import init_alerts_db

SCRIPT = Path(__file__).parent.parent / "scripts" / "purge_expired_keys.py"
_spec = importlib.util.spec_from_file_location("purge_expired_keys", SCRIPT)
purge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(purge)

NOW = datetime(2027, 12, 1, 12, 0, tzinfo=timezone.utc)
CUTOFF = datetime(2026, 12, 1, 12, 0, tzinfo=timezone.utc)
RECENT = (NOW - timedelta(days=10)).isoformat()


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "billing.db"
    monkeypatch.setattr(billing, "DB_PATH", str(path))
    monkeypatch.delenv("BILLING_DB_PATH", raising=False)
    monkeypatch.setattr(purge, "_now", lambda: NOW)
    billing.init_db()
    init_alerts_db()
    return path


def _key(path, name, tier="free", status="active", last_used=RECENT, ended=None, email=None):
    """A hashed key row; name doubles as its key_prefix (8 chars)."""
    key_id = billing.hash_key("csk_" + name + "_full_secret_part")
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO api_keys (api_key, key_prefix, email, tier, status, period_calls_used, period_start, "
            "created_at, last_used_at, ended_at) VALUES (?, ?, ?, ?, ?, 0, 'x', '2025-01-01T00:00:00+00:00', ?, ?)",
            (key_id, name[:8], email or f"{name}@mail.test-shop.dev", tier, status,
             last_used if isinstance(last_used, str) or last_used is None else last_used.isoformat(),
             ended if isinstance(ended, str) or ended is None else ended.isoformat()),
        )
    return key_id


def _watch(path, key_id, events=1):
    with sqlite3.connect(path) as conn:
        watch_id = conn.execute(
            "INSERT INTO watches (api_key, symbol, channel_type, channel_target, created_at) "
            "VALUES (?, 'BTC', 'webhook', 'https://hook.customer.dev/p', 'x')", (key_id,),
        ).lastrowid
        for _ in range(events):
            conn.execute("INSERT INTO alert_events (watch_id, symbol, sent_at) VALUES (?, 'BTC', 'x')", (watch_id,))
    return watch_id


def _keys(path):
    with sqlite3.connect(path) as conn:
        return {r[0] for r in conn.execute("SELECT key_prefix FROM api_keys")}


def _digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest(), Path(path).stat().st_mtime_ns


# --- the cutoff ------------------------------------------------------------------

@pytest.mark.parametrize(
    "now,expected",
    [
        (datetime(2027, 12, 1, 12, 0, tzinfo=timezone.utc), datetime(2026, 12, 1, 12, 0, tzinfo=timezone.utc)),
        (datetime(2028, 2, 29, 9, 30, tzinfo=timezone.utc), datetime(2027, 2, 28, 9, 30, tzinfo=timezone.utc)),
        (datetime(2029, 3, 1, 0, 0, tzinfo=timezone.utc), datetime(2028, 3, 1, 0, 0, tzinfo=timezone.utc)),
    ],
    ids=["plain", "feb-29", "after-leap-year"],
)
def test_twelve_months_before(now, expected):
    assert purge.twelve_months_before(now) == expected


# --- each rule at its boundary --------------------------------------------------

@pytest.mark.parametrize(
    "offset,deleted",
    [(timedelta(days=-1), True), (timedelta(0), True), (timedelta(days=1), False)],
    ids=["day-before-cutoff", "on-cutoff", "day-after-cutoff"],
)
def test_free_idle_boundary(db, offset, deleted):
    _key(db, "freeidle", last_used=CUTOFF + offset)
    assert purge.main(["--db", str(db), "--apply"]) == 0
    assert ("freeidle" not in _keys(db)) is deleted


@pytest.mark.parametrize(
    "offset,deleted",
    [(timedelta(days=-1), True), (timedelta(0), True), (timedelta(days=1), False)],
    ids=["day-before-cutoff", "on-cutoff", "day-after-cutoff"],
)
def test_free_switched_off_boundary(db, offset, deleted):
    _key(db, "freeoff1", status="deactivated", ended=CUTOFF + offset)
    assert purge.main(["--db", str(db), "--apply"]) == 0
    assert ("freeoff1" not in _keys(db)) is deleted


@pytest.mark.parametrize(
    "offset,deleted",
    [(timedelta(days=-1), True), (timedelta(0), True), (timedelta(days=1), False)],
    ids=["day-before-cutoff", "on-cutoff", "day-after-cutoff"],
)
def test_paid_ended_boundary(db, offset, deleted):
    _key(db, "paidend1", tier="pro", status="canceled", ended=CUTOFF + offset)
    assert purge.main(["--db", str(db), "--apply"]) == 0
    assert ("paidend1" not in _keys(db)) is deleted


def test_feb_29_run_uses_feb_28_a_year_earlier(db, monkeypatch):
    monkeypatch.setattr(purge, "_now", lambda: datetime(2028, 2, 29, 12, 0, tzinfo=timezone.utc))
    _key(db, "onfeb28x", last_used="2027-02-28T12:00:00+00:00")
    _key(db, "onmar01x", last_used="2027-03-01T12:00:00+00:00")
    purge.main(["--db", str(db), "--apply"])
    assert _keys(db) == {"onmar01x"}


# --- which keys go ----------------------------------------------------------------

def test_free_key_goes_at_whichever_rule_comes_first(db):
    _key(db, "idlefst1", status="deactivated", last_used=CUTOFF - timedelta(days=5), ended=RECENT)
    _key(db, "offfirst", status="deactivated", last_used=RECENT, ended=CUTOFF - timedelta(days=5))
    _key(db, "activeok", status="active", last_used=RECENT, ended=CUTOFF - timedelta(days=5))  # ended on an active key: not a switch-off
    purge.main(["--db", str(db), "--apply"])
    assert _keys(db) == {"activeok"}


def test_paid_keys_only_go_once_their_subscription_has_ended(db, capsys):
    long_ago = CUTOFF - timedelta(days=200)
    _key(db, "paidactv", tier="pro", status="active", last_used=long_ago)  # idle but paying: kept
    _key(db, "pastdue1", tier="starter", status="canceled", last_used=long_ago, ended=None)  # off, not ended
    _key(db, "paidgone", tier="data", status="canceled", last_used=long_ago, ended=long_ago)
    purge.main(["--db", str(db), "--apply"])
    assert _keys(db) == {"paidactv", "pastdue1"}
    out = capsys.readouterr().out
    assert "paid keys off but not ended (e.g. past_due): 1" in out and "pastdue1" in out


def test_watches_and_events_go_with_their_key_only(db):
    doomed = _key(db, "doomedky", last_used=CUTOFF - timedelta(days=1))
    kept = _key(db, "keptkey1")
    _watch(db, doomed, events=3)
    kept_watch = _watch(db, kept, events=2)
    purge.main(["--db", str(db), "--apply"])
    with sqlite3.connect(db) as conn:
        assert [r[0] for r in conn.execute("SELECT id FROM watches")] == [kept_watch]
        assert conn.execute("SELECT COUNT(*) FROM alert_events").fetchone()[0] == 2
        assert {r[0] for r in conn.execute("SELECT watch_id FROM alert_events")} == {kept_watch}


def test_orphan_watches_are_reported_not_deleted(db, capsys):
    _watch(db, billing.hash_key("csk_gone_key_secret"))
    purge.main(["--db", str(db), "--apply"])
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM watches").fetchone()[0] == 1
    assert "watches whose key no longer exists: 1" in capsys.readouterr().out


# --- dry run, output, and the file --------------------------------------------

def test_dry_run_is_read_only_and_shows_prefixes_only(db, capsys):
    doomed = _key(db, "dryrunk1", last_used=CUTOFF - timedelta(days=1), email="dry-secret@mail.test-shop.dev")
    _watch(db, doomed)
    before = _digest(db)
    assert purge.main(["--db", str(db)]) == 0
    assert _digest(db) == before
    out = capsys.readouterr().out
    assert "free_idle (free key, last used 12+ months ago): 1" in out
    assert "dryrunk1" in out
    assert "would be deleted: 1; their watches: 1; their alert events: 1" in out
    for secret in ("sha256:", "dry-secret", "full_secret_part"):
        assert secret not in out
    assert not Path(str(db) + "-journal").exists()


def test_deleted_rows_are_not_left_in_the_file(db):
    email = "purge-me-please@mail.test-shop.dev"
    doomed = _key(db, "purgeme1", last_used=CUTOFF - timedelta(days=1), email=email)
    assert email.encode() in Path(db).read_bytes()
    purge.main(["--db", str(db), "--apply"])
    raw = Path(db).read_bytes()
    assert email.encode() not in raw and doomed.encode() not in raw


def test_nothing_due_changes_nothing(db, capsys):
    _key(db, "freshkey")
    assert purge.main(["--db", str(db), "--apply"]) == 0
    assert _keys(db) == {"freshkey"}
    assert "Keys deleted: 0" in capsys.readouterr().out


# --- unsafe states stop it ------------------------------------------------------

def _assert_stops(db, capsys, message):
    before = _digest(db)
    for argv in ([], ["--apply"]):
        assert purge.main(["--db", str(db), *argv]) == 1
        out = capsys.readouterr().out
        assert "STOP" in out and message in out and "Nothing changed" in out
    assert _digest(db) == before


def test_stops_on_a_plaintext_key(db, capsys):
    _key(db, "expired1", last_used=CUTOFF - timedelta(days=1))
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO api_keys (api_key, tier, status, period_start, created_at, last_used_at) "
                     "VALUES ('csk_plainLegacyKey_1234567', 'free', 'active', 'x', 'x', ?)", (RECENT,))
    _assert_stops(db, capsys, "hash_existing_keys.py --apply first")


def test_stops_on_a_plaintext_watch(db, capsys):
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO watches (api_key, symbol, channel_type, channel_target, created_at) "
                     "VALUES ('csk_plainWatchKey_1234567', 'BTC', 'webhook', 'https://h.dev/x', 'x')")
    _assert_stops(db, capsys, "hash_existing_keys.py --apply first")


def test_stops_on_a_missing_last_used(db, capsys):
    _key(db, "nolastus", last_used=None)
    _assert_stops(db, capsys, "no last_used_at")


def test_stops_on_an_unreadable_timestamp(db, capsys):
    _key(db, "badstamp", last_used="last tuesday")
    _assert_stops(db, capsys, "can't be read")


def test_stops_when_the_columns_are_missing(tmp_path, capsys):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE api_keys (api_key TEXT PRIMARY KEY, tier TEXT, status TEXT)")
    _assert_stops(path, capsys, "Deploy the hashing release first")


def test_stops_when_the_database_is_missing(tmp_path, capsys):
    missing = tmp_path / "nope.db"
    assert purge.main(["--db", str(missing), "--apply"]) == 1
    assert not missing.exists()
    assert "database not found" in capsys.readouterr().out
