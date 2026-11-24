"""
Delete API keys the privacy policy says are no longer kept, with their alert
watches and alert history. Run it by hand, monthly, from the Render Shell
where the billing database is:

    python scripts/purge_expired_keys.py            # dry run: changes nothing
    python scripts/purge_expired_keys.py --apply    # only after reading the dry run

Rules (cutoff = the same date and time 12 months ago, UTC; Feb 29 -> Feb 28):
  free_idle         free key, last used on or before the cutoff (any status)
  free_switched_off free key, not active, switched off (ended_at) on or before
                    the cutoff
                    -> a free key goes at whichever of these two comes first
  paid_ended        paid key, not active, subscription ended (ended_at) on or
                    before the cutoff
A deleted key takes its watches (any status) and their alert_events with it.

Reported, never deleted:
  - paid keys that are off but whose subscription hasn't ended (ended_at
    empty, e.g. past_due): Stripe will either recover or end them;
  - watches whose key no longer exists.
Not touched: Stripe's own records, the mailbox, and backups (full backups
follow their own 30-day rule, see scripts/backup_data.py).

DRY RUN is the default: it opens the database read-only (sqlite mode=ro) and
prints counts per rule and each key's key_prefix (first 8 characters) only.
Never full keys, hashes or emails.

--apply deletes, in one transaction, alert_events, then watches, then the
api_keys rows, re-checking the rules inside that transaction; then, with
secure_delete on, VACUUM so the deleted rows don't stay in the file. Take a
full backup first: there's no undo except restoring it.

It stops, changing nothing, if: the database file is missing; the
key_prefix / last_used_at / ended_at columns are missing; any key or watch
still holds a plaintext key, or any key has no last_used_at (run
scripts/hash_existing_keys.py --apply first); or a timestamp can't be read.

Database: $BILLING_DB_PATH (default "billing.db"), or --db PATH. The file
must already exist; this script never creates one.
"""

import argparse
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from urllib.parse import quote

HASH_PREFIX = "sha256:"
REQUIRED_COLUMNS = ("key_prefix", "last_used_at", "ended_at")
RULES = (
    ("free_idle", "free key, last used 12+ months ago"),
    ("free_switched_off", "free key, switched off 12+ months ago"),
    ("paid_ended", "paid key, subscription ended 12+ months ago"),
)


class Stop(Exception):
    """An unsafe state: report it and change nothing."""


def _now():
    return datetime.now(timezone.utc)


def twelve_months_before(now):
    """The same calendar date and time a year earlier (Feb 29 -> Feb 28)."""
    try:
        return now.replace(year=now.year - 1)
    except ValueError:
        return now.replace(year=now.year - 1, day=28)


def _parse(ts):
    t = datetime.fromisoformat(ts)
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _shown(prefix):
    return prefix or "(no prefix)"


def _tables(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def plan(conn, now):
    """{rule: [(key_id, prefix)]}, plus what's reported but kept. Raises Stop."""
    tables = _tables(conn)
    if "api_keys" not in tables:
        raise Stop("No api_keys table in this database. Is this the billing database?")
    columns = {r[1] for r in conn.execute("PRAGMA table_info(api_keys)")}
    missing = [c for c in REQUIRED_COLUMNS if c not in columns]
    if missing:
        raise Stop(f"api_keys is missing column(s) {', '.join(missing)}. Deploy the hashing release first.")

    rows = conn.execute(
        "SELECT api_key, key_prefix, tier, status, last_used_at, ended_at FROM api_keys"
    ).fetchall()
    has_watches = "watches" in tables
    has_events = "alert_events" in tables
    watches = conn.execute("SELECT id, api_key FROM watches").fetchall() if has_watches else []

    plaintext = sum(1 for r in rows if not r[0].startswith(HASH_PREFIX))
    plaintext_watches = sum(1 for w in watches if not w[1].startswith(HASH_PREFIX))
    if plaintext or plaintext_watches:
        raise Stop(
            f"{plaintext} key(s) and {plaintext_watches} watch row(s) still hold a plaintext key. "
            "Run scripts/hash_existing_keys.py --apply first."
        )
    no_last_used = [r for r in rows if r[4] is None]
    if no_last_used:
        raise Stop(
            f"{len(no_last_used)} key(s) have no last_used_at "
            f"(prefixes: {' '.join(sorted(_shown(r[1]) for r in no_last_used))}). "
            "Run scripts/hash_existing_keys.py --apply first."
        )

    cutoff = twelve_months_before(now)
    due = defaultdict(list)
    off_not_ended = []
    unreadable = []
    for key_id, prefix, tier, status, last_used_at, ended_at in rows:
        try:
            last_used = _parse(last_used_at)
            ended = _parse(ended_at) if ended_at else None
        except (TypeError, ValueError):
            unreadable.append(_shown(prefix))
            continue
        if tier == "free":
            if last_used <= cutoff:
                due["free_idle"].append((key_id, prefix))
            elif status != "active" and ended is not None and ended <= cutoff:
                due["free_switched_off"].append((key_id, prefix))
        elif status != "active":
            if ended is None:
                off_not_ended.append(_shown(prefix))
            elif ended <= cutoff:
                due["paid_ended"].append((key_id, prefix))
    if unreadable:
        raise Stop(f"{len(unreadable)} key(s) with a timestamp that can't be read "
                   f"(prefixes: {' '.join(sorted(unreadable))}). Look at these by hand.")

    doomed = {key_id for keys in due.values() for key_id, _ in keys}
    doomed_watch_ids = [w[0] for w in watches if w[1] in doomed]
    events = 0
    if has_events and doomed_watch_ids:
        marks = ",".join("?" * len(doomed_watch_ids))
        events = conn.execute(
            f"SELECT COUNT(*) FROM alert_events WHERE watch_id IN ({marks})", doomed_watch_ids
        ).fetchone()[0]
    stored = {r[0] for r in rows}
    return {
        "cutoff": cutoff,
        "due": due,
        "keys_total": len(rows),
        "watches": len(doomed_watch_ids),
        "events": events,
        "off_not_ended": sorted(off_not_ended),
        "orphan_watches": sum(1 for w in watches if w[1] not in stored),
        "has_watches": has_watches,
        "has_events": has_events,
    }


def report(p, applying):
    verb = "deleted" if applying else "would be deleted"
    print(f"Cutoff (12 months ago, UTC): {p['cutoff'].isoformat()}")
    print(f"Keys checked: {p['keys_total']}")
    total = 0
    for name, text in RULES:
        keys = p["due"].get(name, [])
        total += len(keys)
        print(f"  {name} ({text}): {len(keys)}")
        if keys:
            print(f"    {' '.join(sorted(_shown(prefix) for _, prefix in keys))}")
    print(f"Keys {verb}: {total}; their watches: {p['watches']}; their alert events: {p['events']}")
    print(f"Kept: paid keys off but not ended (e.g. past_due): {len(p['off_not_ended'])}")
    if p["off_not_ended"]:
        print(f"    {' '.join(p['off_not_ended'])}")
    print(f"Kept: watches whose key no longer exists: {p['orphan_watches']}")


def delete(conn, p):
    for keys in p["due"].values():
        for key_id, _ in keys:
            if p["has_events"] and p["has_watches"]:
                conn.execute(
                    "DELETE FROM alert_events WHERE watch_id IN (SELECT id FROM watches WHERE api_key = ?)",
                    (key_id,),
                )
            if p["has_watches"]:
                conn.execute("DELETE FROM watches WHERE api_key = ?", (key_id,))
            conn.execute("DELETE FROM api_keys WHERE api_key = ?", (key_id,))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=os.environ.get("BILLING_DB_PATH", "billing.db"))
    parser.add_argument("--apply", action="store_true", help="delete the expired keys (default: dry run)")
    args = parser.parse_args(argv)

    path = os.path.abspath(args.db)
    print("MODE: APPLY (expired keys will be deleted)" if args.apply else "MODE: DRY RUN (nothing will be changed)")
    print(f"Database: {path}")
    if not os.path.isfile(path):
        print(f"STOP: database not found: {path} (set BILLING_DB_PATH or pass --db). Nothing changed.")
        return 1
    now = _now()

    if not args.apply:
        conn = sqlite3.connect(f"file:{quote(path)}?mode=ro", uri=True)
        try:
            p = plan(conn, now)
        except Stop as e:
            print(f"STOP: {e} Nothing changed.")
            return 1
        finally:
            conn.close()
        report(p, applying=False)
        print("DRY RUN: nothing was changed. Re-run with --apply to delete these keys.")
        return 0

    conn = sqlite3.connect(f"file:{quote(path)}?mode=rw", uri=True, timeout=30, isolation_level=None)
    try:
        conn.execute("PRAGMA secure_delete = ON")
        conn.execute("BEGIN IMMEDIATE")
        try:
            p = plan(conn, now)  # re-checked inside the transaction
            delete(conn, p)
        except Stop as e:
            conn.execute("ROLLBACK")
            print(f"STOP: {e} Nothing changed.")
            return 1
        except Exception:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
        report(p, applying=True)
        conn.execute("VACUUM")  # rewrite the file: deleted rows don't stay in free pages
    finally:
        conn.close()
    print("APPLIED.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
