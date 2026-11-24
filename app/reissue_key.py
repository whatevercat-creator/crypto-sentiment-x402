"""
Reissue a subscriber's lost API key (see "How to reissue a key" in
BILLING.md). Run it from the Render Shell, where the billing database lives:

    python -m app.reissue_key --email buyer@example.com
    python -m app.reissue_key --customer cus_ABC123

The key is rotated in place: the same row keeps its plan, Stripe
subscription, usage this month, last-used time and created_at (so an old
/billing/success link stays expired), and the subscriber's alert watches
move to the new key. The old key stops working immediately. The new key is
stored hashed; its plaintext is printed once, here. Matching keys are
listed by their prefix only, and nothing changes until you confirm.
"""

import argparse
import sys

from app.billing import DB_PATH, _db, _new_key, display_prefix, hash_key, is_hashed, key_prefix

_FIELDS = "api_key, key_prefix, email, tier, status, stripe_customer_id, created_at"


def _mask(row) -> str:
    return display_prefix(row["api_key"], row["key_prefix"]) + "..."


def _find(conn, args) -> list:
    if args.customer:
        rows = conn.execute(
            f"SELECT {_FIELDS} FROM api_keys WHERE stripe_customer_id = ? ORDER BY created_at",
            (args.customer,),
        ).fetchall()
    else:
        rows = conn.execute(
            f"SELECT {_FIELDS} FROM api_keys WHERE email = ? COLLATE NOCASE ORDER BY created_at",
            (args.email,),
        ).fetchall()
    if args.key_prefix:
        rows = [r for r in rows if _matches_prefix(r, args.key_prefix)]
    return rows


def _matches_prefix(row, wanted: str) -> bool:
    """The prefix this script prints (key_prefix, 8 characters), or for a
    legacy plaintext row, any longer start of the key."""
    if display_prefix(row["api_key"], row["key_prefix"]).startswith(wanted):
        return True
    return not is_hashed(row["api_key"]) and row["api_key"].startswith(wanted)


def _rotate(conn, old_key_id: str) -> str:
    """Replace the stored key old_key_id (a hash, or a legacy plaintext key)
    with a new key, stored hashed, and move its watches. Returns the new
    key's plaintext."""
    new_key = _new_key()
    new_key_id = hash_key(new_key)
    conn.execute(
        "UPDATE api_keys SET api_key = ?, key_prefix = ? WHERE api_key = ?",
        (new_key_id, key_prefix(new_key), old_key_id),
    )
    has_watches = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'watches'"
    ).fetchone()
    if has_watches:
        conn.execute("UPDATE watches SET api_key = ? WHERE api_key = ?", (new_key_id, old_key_id))
    return new_key


def main(argv=None, confirm=input) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.reissue_key",
        description="Replace a subscriber's lost API key with a new one.",
    )
    who = parser.add_mutually_exclusive_group(required=True)
    who.add_argument("--email", help="email the subscriber used at checkout")
    who.add_argument("--customer", help="Stripe customer ID (cus_...)")
    parser.add_argument("--key-prefix", help="pick one key when several match (e.g. csk_AbC1)")
    parser.add_argument("--yes", action="store_true", help="don't ask for confirmation")
    args = parser.parse_args(argv)

    print(f"Billing database: {DB_PATH}")
    with _db() as conn:
        rows = _find(conn, args)
        if not rows:
            print("No API key matches. Check the email or customer ID in the Stripe dashboard.")
            return 1
        for r in rows:
            print(
                f"  {_mask(r)}  tier={r['tier']}  status={r['status']}  "
                f"email={r['email']}  customer={r['stripe_customer_id']}  issued={r['created_at']}"
            )
        active = [r for r in rows if r["status"] == "active"]
        if not active:
            print("None of these keys is active (the subscription was canceled). Nothing changed.")
            return 1
        if len(active) > 1:
            print("Several active keys match. Rerun with --key-prefix to pick one.")
            return 1
        row = active[0]
        if not args.yes:
            answer = confirm(
                f"Reissue {_mask(row)} ({row['tier']}, {row['email']})? "
                "The old key stops working immediately. [y/N] "
            )
            if answer.strip().lower() not in ("y", "yes"):
                print("Cancelled. Nothing changed.")
                return 1
        new_key = _rotate(conn, row["api_key"])

    print(f"\nNew API key: {new_key}")
    print(
        f"Send it only to {row['email']}, the address on the subscription, and only "
        "in reply to a request from that address."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
