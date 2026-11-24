"""
One-off migration: store existing API keys hashed (see BILLING.md, "How keys
are stored"). Run it from the Render Shell, where the billing database is,
AFTER the hashing release is deployed (its startup adds the columns this
needs):

    python scripts/hash_existing_keys.py            # dry run: changes nothing
    python scripts/hash_existing_keys.py --apply    # only after reading the dry run

Take a full backup first (scripts/backup_data.py). Once this has run, code
older than the hashing release can't read the keys: only fix forward.

DRY RUN is the default: it opens the database read-only (sqlite mode=ro) and
prints counts and key prefixes (first 8 characters) only. Never full keys,
hashes or emails.

--apply does this in one transaction:
  - api_keys rows still holding a plaintext key: api_key becomes
    "sha256:<hex>" of the key, and key_prefix its first 8 characters (if
    empty). The same hash app/billing.py hash_key makes.
  - watches rows holding a plaintext key: api_key becomes its hash, so each
    watch still joins to its key (and a watch written mid-migration with a
    plaintext key is caught on a rerun).
  - last_used_at = the migration time where it's empty: nothing recorded use
    before this release, so the 12-month clock starts now for existing keys.
  - ended_at = the migration time where it's empty on keys that aren't
    active (switched off or canceled before this release).
then, with secure_delete on, VACUUM, so the replaced plaintext doesn't stay
in the file's free pages. Run the dry run again afterwards: it should say 0
plaintext rows. Safe to rerun: rows already hashed are left alone.

It stops, changing nothing, if: the database file is missing; the api_keys
table or the key_prefix / last_used_at / ended_at columns are missing (deploy
the hashing release first); or a plaintext key's hash already exists as
another row.

Database: $BILLING_DB_PATH (default "billing.db"), or --db PATH. The file
must already exist; this script never creates one.
"""

import argparse
import hashlib
import os
import sqlite3
import sys
from datetime import datetime, timezone
from urllib.parse import quote

HASH_PREFIX = "sha256:"
PREFIX_LEN = 8
REQUIRED_COLUMNS = ("key_prefix", "last_used_at", "ended_at")


def hash_key(key):
    """Same as app.billing.hash_key (tests check they agree)."""
    return HASH_PREFIX + hashlib.sha256(key.encode("utf-8")).hexdigest()


def is_hashed(stored):
    return isinstance(stored, str) and stored.startswith(HASH_PREFIX)


def _now():
    return datetime.now(timezone.utc)


class Stop(Exception):
    """An unsafe state: report it and change nothing."""


def _tables(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _columns(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def survey(conn):
    """Everything the dry run reports and --apply acts on. Raises Stop."""
    tables = _tables(conn)
    if "api_keys" not in tables:
        raise Stop("No api_keys table in this database. Is this the billing database?")
    missing = [c for c in REQUIRED_COLUMNS if c not in _columns(conn, "api_keys")]
    if missing:
        raise Stop(
            f"api_keys is missing column(s) {', '.join(missing)}. Deploy the hashing release "
            "first: its startup adds them."
        )

    keys = conn.execute("SELECT api_key, key_prefix, status, last_used_at, ended_at FROM api_keys").fetchall()
    stored = {r[0] for r in keys}
    plain_keys = [r[0] for r in keys if not is_hashed(r[0])]
    collisions = [k for k in plain_keys if hash_key(k) in stored]
    if collisions:
        raise Stop(
            f"{len(collisions)} plaintext key(s) whose hash already exists as another row "
            f"(prefixes: {' '.join(sorted(k[:PREFIX_LEN] for k in collisions))}). Look at these by hand."
        )

    has_watches = "watches" in tables
    watches = conn.execute("SELECT id, api_key FROM watches").fetchall() if has_watches else []
    plain_watch_values = sorted({w[1] for w in watches if not is_hashed(w[1])})
    known = stored | {hash_key(k) for k in plain_keys}
    orphan_watches = [w for w in watches if w[1] not in known and hash_key(w[1]) not in known]

    return {
        "keys_total": len(keys),
        "plain_keys": sorted(plain_keys),
        "hashed_keys": len(keys) - len(plain_keys),
        "has_watches": has_watches,
        "watches_total": len(watches),
        "plain_watch_rows": sum(1 for w in watches if not is_hashed(w[1])),
        "plain_watch_values": plain_watch_values,
        "orphan_watches": len(orphan_watches),
        "no_last_used": sum(1 for r in keys if r[3] is None),
        "off_no_ended": sum(1 for r in keys if r[2] != "active" and r[4] is None),
    }


def report(s, applying):
    would = "" if applying else "would be "
    print(f"api_keys: {s['keys_total']} rows; plaintext: {len(s['plain_keys'])}; hashed: {s['hashed_keys']}")
    if s["plain_keys"]:
        print(f"  plaintext keys {would}hashed (prefixes): {' '.join(k[:PREFIX_LEN] for k in s['plain_keys'])}")
    if s["has_watches"]:
        print(
            f"watches: {s['watches_total']} rows; plaintext: {s['plain_watch_rows']} "
            f"({len(s['plain_watch_values'])} distinct keys); not matching any key: {s['orphan_watches']}"
        )
    else:
        print("watches: no table (nothing to change)")
    print(f"last_used_at empty: {s['no_last_used']} ({would}set to the migration time)")
    print(f"not active with ended_at empty: {s['off_no_ended']} ({would}set to the migration time)")


def apply(conn, s, now_iso):
    for key in s["plain_keys"]:
        conn.execute(
            "UPDATE api_keys SET api_key = ?, key_prefix = COALESCE(key_prefix, ?) WHERE api_key = ?",
            (hash_key(key), key[:PREFIX_LEN], key),
        )
    if s["has_watches"]:
        for value in s["plain_watch_values"]:
            conn.execute("UPDATE watches SET api_key = ? WHERE api_key = ?", (hash_key(value), value))
    conn.execute("UPDATE api_keys SET last_used_at = ? WHERE last_used_at IS NULL", (now_iso,))
    conn.execute(
        "UPDATE api_keys SET ended_at = ? WHERE status != 'active' AND ended_at IS NULL", (now_iso,)
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=os.environ.get("BILLING_DB_PATH", "billing.db"))
    parser.add_argument("--apply", action="store_true", help="hash the keys (default: dry run)")
    args = parser.parse_args(argv)

    path = os.path.abspath(args.db)
    print("MODE: APPLY (keys will be hashed in place)" if args.apply else "MODE: DRY RUN (nothing will be changed)")
    print(f"Database: {path}")
    if not os.path.isfile(path):
        print(f"STOP: database not found: {path} (set BILLING_DB_PATH or pass --db). Nothing changed.")
        return 1

    if not args.apply:
        conn = sqlite3.connect(f"file:{quote(path)}?mode=ro", uri=True)
        try:
            s = survey(conn)
        except Stop as e:
            print(f"STOP: {e} Nothing changed.")
            return 1
        finally:
            conn.close()
        report(s, applying=False)
        print("DRY RUN: nothing was changed. Re-run with --apply to hash these keys.")
        return 0

    now_iso = _now().isoformat()
    conn = sqlite3.connect(f"file:{quote(path)}?mode=rw", uri=True, timeout=30, isolation_level=None)
    try:
        conn.execute("PRAGMA secure_delete = ON")
        conn.execute("BEGIN IMMEDIATE")  # the app waits while this runs; it's quick
        try:
            s = survey(conn)
            apply(conn, s, now_iso)
        except Stop as e:
            conn.execute("ROLLBACK")
            print(f"STOP: {e} Nothing changed.")
            return 1
        except Exception:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
        report(s, applying=True)
        conn.execute("VACUUM")  # rewrite the file: no old plaintext left in free pages
        after = survey(conn)
    finally:
        conn.close()
    print(f"APPLIED at {now_iso}. After: plaintext keys {len(after['plain_keys'])}, "
          f"plaintext watch rows {after['plain_watch_rows']}.")
    print("Run the dry run again: it should report 0 plaintext rows.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
