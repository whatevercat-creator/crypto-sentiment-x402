"""
Weekly off-Render backup of the SQLite data on the Render disk (/data).
Run it from a Render SSH session; it only reads /data:

    python scripts/backup_data.py                  # full copies (contain personal data)
    python scripts/backup_data.py --archive-only   # archive tables only, no personal data

What it does:
  - Finds the SQLite files: every file under --data-dir (default /data)
    whose header says SQLite, or only the files given with --db PATH.
    If $BILLING_DB_PATH is set it must be one of them. A missing source is
    an error: this script never creates a database.
  - Opens each source READ-ONLY (sqlite "mode=ro") and copies it with the
    sqlite3 backup API into /tmp/backup-<UTC date>/ (or --out DIR). The copy
    is consistent even while the app is writing. Reading takes SQLite's
    normal shared read lock for a moment at a time (the copy goes in small
    steps), the same as any read the app does itself; nothing under /data
    is written, created or changed.
  - Runs PRAGMA integrity_check on each copy, prints row counts per table
    and the SHA-256 of each copy, and writes them to SHA256SUMS in the
    output folder (check later with: shasum -a 256 -c SHA256SUMS).
  - Prints a warning (and carries on) if started within 5 minutes of the
    top of the hour, when the app inserts its hourly rows. Best run between
    :15 and :45 past the hour.

--archive-only writes a new file <name>-archive.db holding only the archive
tables (ARCHIVE_TABLES below, with their indexes), read in one consistent
transaction. No API keys, emails, Stripe IDs or alert channels go into it,
and nothing else is written to disk.

The full copy contains customer emails, plaintext API keys, Stripe IDs and
alert webhook URLs. Keep it off git, chat, cloud drives and email.
"""

import argparse
import hashlib
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from urllib.parse import quote

# The sentiment archive: can't be backfilled, holds no personal data.
ARCHIVE_TABLES = ("sentiment_hourly", "sentiment_snapshots", "sentiment_headlines")

SQLITE_HEADER = b"SQLite format 3\x00"
SIDECAR_SUFFIXES = ("-journal", "-wal", "-shm")
PAGES_PER_STEP = 256  # backup API step size; the read lock is released between steps
STEP_PAUSE_SECONDS = 0.01
TOP_OF_HOUR_MARGIN_MINUTES = 5  # warn when started this close to the hourly insert at :00


def _is_sqlite(path):
    try:
        with open(path, "rb") as f:
            return f.read(len(SQLITE_HEADER)) == SQLITE_HEADER
    except OSError:
        return False


def find_sources(data_dir, explicit, billing_db_path):
    if explicit:
        sources = [os.path.abspath(p) for p in explicit]
        for p in sources:
            if not os.path.isfile(p):
                sys.exit(f"Source not found: {p}. Nothing copied.")
            if not _is_sqlite(p):
                sys.exit(f"Not a SQLite file: {p}. Nothing copied.")
    else:
        if not os.path.isdir(data_dir):
            sys.exit(f"Data folder not found: {data_dir}. Nothing copied.")
        sources = []
        for root, dirs, files in os.walk(data_dir, onerror=lambda e: print(f"skipped (unreadable): {e.filename}")):
            dirs.sort()
            for name in sorted(files):
                path = os.path.join(root, name)
                if name.endswith(SIDECAR_SUFFIXES) or os.path.islink(path):
                    continue
                if _is_sqlite(path):
                    sources.append(os.path.abspath(path))
        if not sources:
            sys.exit(f"No SQLite files under {data_dir}. Nothing copied.")
    if billing_db_path:
        want = os.path.abspath(billing_db_path)
        if not os.path.isfile(want):
            sys.exit(f"BILLING_DB_PATH file not found: {want}. Nothing copied.")
        if want not in sources:
            sys.exit(f"BILLING_DB_PATH ({want}) is not among the sources. Nothing copied.")
    return sources


def _ro_uri(path):
    return f"file:{quote(os.path.abspath(path))}?mode=ro"


def _open_ro(path):
    return sqlite3.connect(_ro_uri(path), uri=True)


def _tables(conn, schema="main"):
    return [
        r[0]
        for r in conn.execute(
            f"SELECT name FROM {schema}.sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]


def full_copy(src_path, dest_path):
    src = _open_ro(src_path)
    dest = sqlite3.connect(dest_path)
    try:
        src.backup(dest, pages=PAGES_PER_STEP, sleep=STEP_PAUSE_SECONDS)
    finally:
        dest.close()
        src.close()


def archive_copy(src_path, dest_path):
    """Copy only ARCHIVE_TABLES (and their indexes) into a new file, in one
    read transaction so the tables are consistent with each other."""
    # uri=True on this connection too, so ATTACH reads the source URI as
    # read-only instead of treating "file:...?mode=ro" as a new filename.
    dest = sqlite3.connect(
        f"file:{quote(os.path.abspath(dest_path))}?mode=rwc", uri=True, isolation_level=None
    )
    try:
        dest.execute("ATTACH DATABASE ? AS src", (_ro_uri(src_path),))
        present = set(_tables(dest, "src"))
        missing = [t for t in ARCHIVE_TABLES if t not in present]
        if missing:
            raise SystemExit(f"Archive tables missing from {src_path}: {', '.join(missing)}. Nothing copied.")
        dest.execute("BEGIN")
        for table in ARCHIVE_TABLES:
            (create_sql,) = dest.execute(
                "SELECT sql FROM src.sqlite_master WHERE type = 'table' AND name = ?", (table,)
            ).fetchone()
            dest.execute(create_sql)
            dest.execute(f'INSERT INTO main."{table}" SELECT * FROM src."{table}"')
            for (index_sql,) in dest.execute(
                "SELECT sql FROM src.sqlite_master WHERE type = 'index' AND tbl_name = ? "
                "AND sql IS NOT NULL ORDER BY name",
                (table,),
            ).fetchall():
                dest.execute(index_sql)
        dest.execute("COMMIT")
        dest.execute("DETACH DATABASE src")
    except BaseException:
        dest.close()
        if os.path.exists(dest_path):
            os.remove(dest_path)
        raise
    dest.close()


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def check_copy(path):
    """(integrity_check result, {table: row count}) for a finished copy."""
    conn = _open_ro(path)
    try:
        integrity = "; ".join(r[0] for r in conn.execute("PRAGMA integrity_check"))
        counts = {t: conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in _tables(conn)}
    finally:
        conn.close()
    return integrity, counts


def _now():
    return datetime.now(timezone.utc)


def near_top_of_hour(now, margin_minutes=TOP_OF_HOUR_MARGIN_MINUTES):
    """True within margin_minutes either side of :00, when the app's hourly
    insert runs. (US Eastern is a whole-hour offset from UTC, so the minute
    is the same in both.)"""
    return now.minute >= 60 - margin_minutes or now.minute < margin_minutes


def _inside(path, folder):
    path, folder = os.path.realpath(path), os.path.realpath(folder)
    return path == folder or path.startswith(folder.rstrip(os.sep) + os.sep)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-dir", default="/data", help="folder to scan for SQLite files (default /data)")
    parser.add_argument("--db", action="append", metavar="PATH", help="back up only this file (repeatable)")
    parser.add_argument("--out", help="output folder (default /tmp/backup-<UTC date>)")
    parser.add_argument("--archive-only", action="store_true", help="copy only the archive tables, no personal data")
    args = parser.parse_args(argv)

    started_at = _now()
    if near_top_of_hour(started_at):
        print(
            f"WARNING: started at :{started_at.minute:02d}, within {TOP_OF_HOUR_MARGIN_MINUTES} minutes "
            "of the top of the hour, when the app writes its hourly rows. The copy is still "
            "consistent, but best run between :15 and :45 past the hour."
        )
    sources = find_sources(args.data_dir, args.db, os.environ.get("BILLING_DB_PATH"))
    out = os.path.abspath(args.out or f"/tmp/backup-{started_at:%Y-%m-%d}")
    for s in sources:
        if _inside(out, os.path.dirname(s)):
            sys.exit(f"Output folder {out} is inside the data folder {os.path.dirname(s)}. Nothing copied.")
    if args.db is None and _inside(out, args.data_dir):
        sys.exit(f"Output folder {out} is inside {args.data_dir}. Nothing copied.")

    print("MODE: ARCHIVE ONLY (archive tables, no personal data)" if args.archive_only
          else "MODE: FULL (copies contain emails, API keys, Stripe IDs, alert channels)")
    print(f"Sources (opened read-only): {', '.join(sources)}")
    print(f"Output: {out}")

    plan = []
    for src in sources:
        base = os.path.basename(src)
        if args.archive_only:
            conn = _open_ro(src)
            try:
                has_archive = any(t in ARCHIVE_TABLES for t in _tables(conn))
            finally:
                conn.close()
            if not has_archive:
                print(f"skipped {src}: no archive tables")
                continue
            stem, ext = os.path.splitext(base)
            base = f"{stem}-archive{ext or '.db'}"
        dest = os.path.join(out, base)
        if os.path.exists(dest):
            sys.exit(f"Already exists: {dest} (remove it or pass --out). Nothing copied.")
        plan.append((src, dest))
    if not plan:
        sys.exit("Nothing to copy.")

    os.makedirs(out, mode=0o700, exist_ok=True)
    os.umask(0o077)
    sums, failed = [], False
    for src, dest in plan:
        started = time.monotonic()
        (archive_copy if args.archive_only else full_copy)(src, dest)
        os.chmod(dest, 0o600)
        integrity, counts = check_copy(dest)
        digest = sha256_of(dest)
        sums.append(f"{digest}  {os.path.basename(dest)}\n")
        print()
        print(f"{src} -> {dest} ({os.path.getsize(dest):,} bytes, {time.monotonic() - started:.1f}s)")
        print(f"  integrity_check: {integrity}")
        for table, n in counts.items():
            print(f"  {table}: {n:,} rows")
        print(f"  sha256: {digest}")
        if integrity != "ok":
            failed = True
    sums_path = os.path.join(out, "SHA256SUMS")
    with open(sums_path, "a") as f:
        f.writelines(sums)
    os.chmod(sums_path, 0o600)
    print()
    print(f"Wrote {sums_path}")
    if failed:
        sys.exit("integrity_check FAILED on at least one copy (see above).")
    print("Done. Nothing under the data folder was changed.")


if __name__ == "__main__":
    main()
