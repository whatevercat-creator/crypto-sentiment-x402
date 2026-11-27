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
    sqlite3 backup API into the output folder (or --out DIR):
      full:          /tmp/backup-FULL-<UTC date>-delete-by-<UTC date + 30 days>/
      archive-only:  /tmp/backup-ARCHIVE-<UTC date>/
    The copy is consistent even while the app is writing. Reading takes SQLite's
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

The full copy contains customer emails, API keys, Stripe IDs and alert
webhook URLs. Keys are stored hashed once scripts/hash_existing_keys.py
--apply has run; a full copy made before that holds them in plaintext, so
delete those copies after the migration. Keep every full copy off git, chat,
cloud drives and email.

30-day rule (Forge Alone privacy policy): a full copy is kept for at most
FULL_RETENTION_DAYS days, then deleted. A full run therefore puts the
delete-by date in the folder name, writes DELETE-BY.txt into the folder
before copying anything (so even a failed run's folder is marked), and
prints "DELETE BY <date>" at the start and at the end. If DELETE-BY.txt is
already there (another full run into the same --out), the earlier date is
kept. Archive-only copies hold no personal data and have no delete-by date.
"""

import argparse
import hashlib
import os
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote

# The sentiment archive: can't be backfilled, holds no personal data.
# news_items (the rolling 72-hour story store, app/news_store.py) is left out
# on purpose: it holds no personal data either, but it's a cache that refills
# itself from the feeds and deletes its own rows after 73 hours, the matched
# headlines are already kept in sentiment_headlines, and listing it here
# would make --archive-only stop on a database created before the store.
ARCHIVE_TABLES = ("sentiment_hourly", "sentiment_snapshots", "sentiment_headlines")

SQLITE_HEADER = b"SQLite format 3\x00"
SIDECAR_SUFFIXES = ("-journal", "-wal", "-shm")
PAGES_PER_STEP = 256  # backup API step size; the read lock is released between steps
STEP_PAUSE_SECONDS = 0.01
TOP_OF_HOUR_MARGIN_MINUTES = 5  # warn when started this close to the hourly insert at :00
FULL_RETENTION_DAYS = 30  # privacy policy: full copies (personal data) are deleted within this
DELETE_BY_FILE = "DELETE-BY.txt"


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


def delete_by_date(started_at):
    """The date a full copy made at started_at (UTC) must be deleted by."""
    return started_at.astimezone(timezone.utc).date() + timedelta(days=FULL_RETENTION_DAYS)


def default_out(started_at, archive_only):
    """The default output folder: the kind of copy and its dates in the name."""
    made = started_at.astimezone(timezone.utc).date()
    if archive_only:
        return f"/tmp/backup-ARCHIVE-{made:%Y-%m-%d}"
    return f"/tmp/backup-FULL-{made:%Y-%m-%d}-delete-by-{delete_by_date(started_at):%Y-%m-%d}"


def delete_by_banner(delete_by):
    return (
        f"DELETE BY {delete_by:%Y-%m-%d} ({FULL_RETENTION_DAYS} days): "
        "this copy contains personal data."
    )


def _existing_delete_by(path):
    """The date in an existing DELETE-BY.txt, or None if it can't be read."""
    try:
        with open(path) as f:
            first = f.readline().strip()
        return date.fromisoformat(first.removeprefix("DELETE BY:").strip())
    except (OSError, ValueError):
        return None


def write_delete_by(out, started_at):
    """Write DELETE-BY.txt into a full backup's folder and return the
    delete-by date it holds. An existing file's earlier date wins, so
    adding to a folder never pushes its deadline back."""
    path = os.path.join(out, DELETE_BY_FILE)
    delete_by = delete_by_date(started_at)
    existing = _existing_delete_by(path)
    if existing is not None and existing < delete_by:
        delete_by = existing
    with open(path, "w") as f:
        f.write(
            f"DELETE BY: {delete_by:%Y-%m-%d}\n"
            f"Made: {started_at.astimezone(timezone.utc):%Y-%m-%dT%H:%M:%SZ} (UTC)\n"
            "This folder is a FULL backup: it contains personal data (customer emails, "
            "API keys, Stripe IDs, alert channels).\n"
            f"Delete this whole folder, and every copy of it, by the date above "
            f"({FULL_RETENTION_DAYS} days after it was made).\n"
        )
    os.chmod(path, 0o600)
    return delete_by


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
    parser.add_argument(
        "--out",
        help="output folder (default /tmp/backup-FULL-<UTC date>-delete-by-<date+30>, "
        "or /tmp/backup-ARCHIVE-<UTC date> with --archive-only)",
    )
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
    out = os.path.abspath(args.out or default_out(started_at, args.archive_only))
    for s in sources:
        if _inside(out, os.path.dirname(s)):
            sys.exit(f"Output folder {out} is inside the data folder {os.path.dirname(s)}. Nothing copied.")
    if args.db is None and _inside(out, args.data_dir):
        sys.exit(f"Output folder {out} is inside {args.data_dir}. Nothing copied.")

    print("MODE: ARCHIVE ONLY (archive tables, no personal data)" if args.archive_only
          else "MODE: FULL (copies contain emails, API keys, Stripe IDs, alert channels)")
    retention_line = (
        "No personal data: no delete-by date." if args.archive_only
        else delete_by_banner(delete_by_date(started_at))
    )
    print(retention_line)
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
    if not args.archive_only:
        # Before any copy, so even a failed run's folder carries its deadline.
        retention_line = delete_by_banner(write_delete_by(out, started_at))
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
    print(retention_line)
    if failed:
        sys.exit("integrity_check FAILED on at least one copy (see above).")
    print("Done. Nothing under the data folder was changed.")


if __name__ == "__main__":
    main()
