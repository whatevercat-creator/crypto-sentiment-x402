"""scripts/backup_data.py against temporary SQLite files built with the app's
real schemas: copies match row counts, sources stay untouched (mtime, hash,
no sidecar files), --archive-only leaves out every billing/alerts table, and
missing sources are refused without creating anything."""

import hashlib
import importlib.util
import os
import sqlite3
from pathlib import Path

import pytest

import tests.test_home  # noqa: F401  (env vars app.billing needs)
from app import billing
from app.alerts import init_alerts_db
from app.dataset import init_dataset_db
from app.hourly import init_hourly_db

SCRIPT = Path(__file__).parent.parent / "scripts" / "backup_data.py"
_spec = importlib.util.spec_from_file_location("backup_data", SCRIPT)
backup = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(backup)

EMAIL = "customer-private@example.org"
KEY = "csk_privatekey_do_not_copy_0123456789"
WEBHOOK = "https://discord.com/api/webhooks/123/secret-token"
PERSONAL_TABLES = {"api_keys", "watches", "alert_events"}


@pytest.fixture
def data(tmp_path, monkeypatch):
    """A /data stand-in holding billing.db with every app table filled."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    path = data_dir / "billing.db"
    monkeypatch.setattr(billing, "DB_PATH", str(path))
    monkeypatch.delenv("BILLING_DB_PATH", raising=False)
    billing.init_db()
    init_alerts_db()
    init_dataset_db()
    init_hourly_db()
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO api_keys (api_key, email, tier, status, period_calls_used, period_start, created_at) "
            "VALUES (?, ?, 'pro', 'active', 0, '2026-11-01T00:00:00Z', '2026-11-01T00:00:00Z')",
            (KEY, EMAIL),
        )
        conn.execute(
            "INSERT INTO watches (api_key, symbol, channel_type, channel_target, created_at) "
            "VALUES (?, 'BTC', 'discord', ?, '2026-11-01T00:00:00Z')",
            (KEY, WEBHOOK),
        )
        conn.execute(
            "INSERT INTO alert_events (watch_id, symbol, sent_at) VALUES (1, 'BTC', '2026-11-01T00:00:00Z')"
        )
        for h in range(50):
            conn.execute(
                "INSERT INTO sentiment_hourly (symbol, observed_at, average_compound, sample_size) "
                "VALUES (?, ?, 0.1, 20)",
                ("BTC" if h % 2 else "ETH", f"2026-11-0{1 + h // 24}T{h % 24:02d}:00:00+00:00"),
            )
        for d in range(1, 8):
            conn.execute(
                "INSERT INTO sentiment_snapshots (symbol, snapshot_date, label, average_compound, created_at) "
                "VALUES ('BTC', ?, 'neutral', 0.0, ?)",
                (f"2026-11-0{d}", f"2026-11-0{d}T00:00:00Z"),
            )
        conn.execute(
            "INSERT INTO sentiment_headlines (link, title, source, first_seen, score, symbols) "
            "VALUES ('https://news.example/a', 'BTC up', 'news', '2026-11-01', 0.5, 'BTC')"
        )
    return data_dir


def _counts(path):
    return backup.check_copy(str(path))[1]


def _state(folder):
    """Every file under folder with its mtime and SHA-256."""
    out = {}
    for p in sorted(Path(folder).rglob("*")):
        if p.is_file():
            out[p.name] = (p.stat().st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest())
    return out


def test_full_copy_matches_and_leaves_source_untouched(data, tmp_path, capsys):
    before = _state(data)
    src_counts = _counts(data / "billing.db")
    out = tmp_path / "out"

    backup.main(["--data-dir", str(data), "--out", str(out)])

    assert _state(data) == before  # same files, mtimes and hashes; no -journal left behind
    copy = out / "billing.db"
    assert _counts(copy) == src_counts
    assert set(src_counts) >= PERSONAL_TABLES | set(backup.ARCHIVE_TABLES)
    printed = capsys.readouterr().out
    digest = hashlib.sha256(copy.read_bytes()).hexdigest()
    assert f"sha256: {digest}" in printed
    assert "integrity_check: ok" in printed
    assert "sentiment_hourly: 50 rows" in printed
    assert (out / "SHA256SUMS").read_text() == f"{digest}  billing.db\n"
    assert oct(copy.stat().st_mode & 0o777) == "0o600"


def test_archive_only_excludes_billing_tables(data, tmp_path, capsys):
    before = _state(data)
    src_counts = _counts(data / "billing.db")
    out = tmp_path / "out"

    backup.main(["--data-dir", str(data), "--out", str(out), "--archive-only"])

    assert _state(data) == before
    assert sorted(os.listdir(out)) == ["SHA256SUMS", "billing-archive.db"]
    copy = out / "billing-archive.db"
    counts = _counts(copy)
    assert set(counts) == set(backup.ARCHIVE_TABLES)
    assert counts == {t: src_counts[t] for t in backup.ARCHIVE_TABLES}
    raw = copy.read_bytes()
    for secret in (EMAIL, KEY, WEBHOOK):
        assert secret.encode() not in raw
    conn = sqlite3.connect(copy)
    indexes = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
    conn.close()
    assert {"idx_hourly_symbol_time", "idx_snapshots_symbol_date", "idx_headlines_published"} <= indexes
    assert "integrity_check: ok" in capsys.readouterr().out


def test_copy_is_consistent_while_a_write_is_pending(data, tmp_path):
    """An uncommitted write on another connection doesn't block the copy and
    doesn't leak into it."""
    writer = sqlite3.connect(data / "billing.db", isolation_level=None)
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("INSERT INTO sentiment_hourly (symbol, observed_at) VALUES ('BTC', '2026-12-01T00:00:00+00:00')")
    try:
        backup.main(["--data-dir", str(data), "--out", str(tmp_path / "full")])
        backup.main(["--data-dir", str(data), "--out", str(tmp_path / "arch"), "--archive-only"])
    finally:
        writer.execute("ROLLBACK")
        writer.close()
    assert _counts(tmp_path / "full" / "billing.db")["sentiment_hourly"] == 50
    assert _counts(tmp_path / "arch" / "billing-archive.db")["sentiment_hourly"] == 50


def test_missing_source_is_refused_and_nothing_created(tmp_path, monkeypatch):
    monkeypatch.delenv("BILLING_DB_PATH", raising=False)
    missing = tmp_path / "data" / "billing.db"
    out = tmp_path / "out"
    with pytest.raises(SystemExit, match="Source not found"):
        backup.main(["--db", str(missing), "--out", str(out)])
    with pytest.raises(SystemExit, match="Data folder not found"):
        backup.main(["--data-dir", str(tmp_path / "data"), "--out", str(out)])
    (tmp_path / "data").mkdir()
    with pytest.raises(SystemExit, match="No SQLite files"):
        backup.main(["--data-dir", str(tmp_path / "data"), "--out", str(out)])
    assert not missing.exists()
    assert not out.exists()


def test_missing_billing_db_path_is_refused(data, tmp_path, monkeypatch):
    monkeypatch.setenv("BILLING_DB_PATH", str(data / "elsewhere.db"))
    with pytest.raises(SystemExit, match="BILLING_DB_PATH file not found"):
        backup.main(["--data-dir", str(data), "--out", str(tmp_path / "out")])
    assert not (data / "elsewhere.db").exists()
    monkeypatch.setenv("BILLING_DB_PATH", str(data / "billing.db"))
    backup.main(["--data-dir", str(data), "--out", str(tmp_path / "out")])


def test_archive_only_refuses_when_archive_tables_missing(tmp_path, monkeypatch):
    monkeypatch.delenv("BILLING_DB_PATH", raising=False)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    with sqlite3.connect(data_dir / "billing.db") as conn:
        conn.execute("CREATE TABLE sentiment_hourly (id INTEGER)")
    out = tmp_path / "out"
    with pytest.raises(SystemExit, match="Archive tables missing"):
        backup.main(["--data-dir", str(data_dir), "--out", str(out), "--archive-only"])
    assert not (out / "billing-archive.db").exists()


def test_output_inside_data_folder_is_refused(data):
    before = _state(data)
    with pytest.raises(SystemExit, match="inside"):
        backup.main(["--data-dir", str(data), "--out", str(data / "backup")])
    assert _state(data) == before


def test_existing_copy_is_never_overwritten(data, tmp_path):
    out = tmp_path / "out"
    backup.main(["--data-dir", str(data), "--out", str(out)])
    first = (out / "billing.db").read_bytes()
    with pytest.raises(SystemExit, match="Already exists"):
        backup.main(["--data-dir", str(data), "--out", str(out)])
    assert (out / "billing.db").read_bytes() == first


def test_sidecar_and_non_sqlite_files_are_skipped(data, tmp_path):
    (data / "billing.db-journal").write_bytes(b"")
    (data / "notes.txt").write_text("not a database")
    assert backup.find_sources(str(data), None, None) == [str((data / "billing.db").resolve())]
