"""
Reissue a subscriber's lost API key (see "How to reissue a key" in
BILLING.md). Run it from the Render Shell, where the billing database lives:

    python -m app.reissue_key --email buyer@example.com
    python -m app.reissue_key --customer cus_ABC123

The key is rotated in place: the same row keeps its plan, Stripe
subscription, usage this month and created_at (so an old /billing/success
link stays expired), and the subscriber's alert watches move to the new
key. The old key stops working immediately. Matching keys are listed
masked, and nothing changes until you confirm.
"""

import argparse
import sys

from app.billing import DB_PATH, _db, _new_key

_FIELDS = "api_key, email, tier, status, stripe_customer_id, created_at"


def _mask(key: str) -> str:
    return key[:8] + "..."


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
        rows = [r for r in rows if r["api_key"].startswith(args.key_prefix)]
    return rows


def _rotate(conn, old_key: str) -> str:
    new_key = _new_key()
    conn.execute("UPDATE api_keys SET api_key = ? WHERE api_key = ?", (new_key, old_key))
    has_watches = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'watches'"
    ).fetchone()
    if has_watches:
        conn.execute("UPDATE watches SET api_key = ? WHERE api_key = ?", (new_key, old_key))
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
                f"  {_mask(r['api_key'])}  tier={r['tier']}  status={r['status']}  "
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
                f"Reissue {_mask(row['api_key'])} ({row['tier']}, {row['email']})? "
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
