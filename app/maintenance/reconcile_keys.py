"""
One-off: make paid API keys match Stripe. Run it from the Render Shell, where
the billing database and live Stripe key are (see BILLING.md):

    python -m app.maintenance.reconcile_keys           # dry run, writes nothing
    python -m app.maintenance.reconcile_keys --apply

For every non-free key it asks Stripe whether there is an active subscription
to one of this service's plans. A key with none is marked canceled, and active
watches of keys that aren't active are marked inactive.

Stripe calls are read-only. Output is counts and the first 6 characters of
keys, never full keys, emails or IDs. A key Stripe couldn't answer for is left
alone, and a canceled key is never turned back on: it's reported instead.
"""

import argparse
import os
import sqlite3
import sys
from urllib.parse import quote

import stripe

from app import billing

KEEP = "keep"
NO_SUBSCRIPTION = "no_subscription"
UNKNOWN = "unknown"


def _prefix(key: str) -> str:
    return key[:6]


def _is_active_plan(subscription: dict) -> bool:
    return (
        subscription.get("status") in billing.ACTIVE_SUBSCRIPTION_STATUSES
        and billing.tier_for_items(subscription.get("items")) is not None
    )


def _stripe_verdict(row) -> str:
    subscription_id = row["stripe_subscription_id"]
    customer_id = row["stripe_customer_id"]
    try:
        if subscription_id:
            subscriptions = [stripe.Subscription.retrieve(subscription_id).to_dict()]
        elif customer_id:
            listed = stripe.Subscription.list(customer=customer_id, status="all", limit=100).to_dict()
            subscriptions = listed.get("data") or []
        else:
            return NO_SUBSCRIPTION
    except stripe.InvalidRequestError as e:
        # Includes a test-mode object looked up with a live key.
        return NO_SUBSCRIPTION if e.code == "resource_missing" else UNKNOWN
    except stripe.StripeError:
        return UNKNOWN
    return KEEP if any(_is_active_plan(s) for s in subscriptions) else NO_SUBSCRIPTION


def _connect(apply: bool) -> sqlite3.Connection:
    if apply:
        return billing._db()
    # Read-only at the database level, so a dry run can't write.
    conn = sqlite3.connect(f"file:{quote(os.path.abspath(billing.DB_PATH))}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _active_watches(conn) -> list:
    """(watch id, its key, whether that key is active) for active watches."""
    has_watches = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'watches'"
    ).fetchone()
    if not has_watches:
        return []
    return conn.execute(
        "SELECT w.id, w.api_key, k.status AS key_status FROM watches w "
        "LEFT JOIN api_keys k ON k.api_key = w.api_key WHERE w.status = 'active'"
    ).fetchall()


def _report(title: str, keys: list) -> None:
    print(f"  {title}: {len(keys)}")
    if keys:
        print(f"    {' '.join(sorted(_prefix(k) for k in keys))}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.maintenance.reconcile_keys",
        description="Mark paid keys canceled when Stripe has no active subscription for them.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="report only (the default)")
    mode.add_argument("--apply", action="store_true", help="write the changes")
    args = parser.parse_args(argv)

    # These would make real subscribers look like non-subscribers.
    if not stripe.api_key:
        print("STRIPE_SECRET_KEY is not set. Nothing checked.")
        return 1
    missing = billing.missing_price_envs()
    if missing:
        print(f"Plan prices not set: {', '.join(missing)}. Nothing checked.")
        return 1
    live = stripe.api_key.startswith(("sk_live_", "rk_live_"))
    if args.apply and not live:
        print("--apply needs a live Stripe key: a test key can't see live subscriptions. Nothing checked.")
        return 1
    if not os.path.exists(billing.DB_PATH):
        print("Billing database not found. Nothing checked.")
        return 1

    print(f"Billing database: {billing.DB_PATH}")
    print(f"Stripe key: {'live' if live else 'TEST (live subscriptions will look missing)'}")
    print("Mode: " + ("APPLY" if args.apply else "DRY RUN (nothing is written)"))

    conn = _connect(args.apply)
    try:
        rows = conn.execute(
            "SELECT api_key, status, stripe_customer_id, stripe_subscription_id "
            "FROM api_keys WHERE tier != 'free'"
        ).fetchall()

        keep, to_cancel, already, drifted, unknown = [], [], [], [], []
        for row in rows:
            verdict = _stripe_verdict(row)
            key, is_active = row["api_key"], row["status"] == "active"
            if verdict == UNKNOWN:
                unknown.append(key)
            elif verdict == KEEP:
                (keep if is_active else drifted).append(key)
            else:
                (to_cancel if is_active else already).append(key)

        canceling = set(to_cancel)
        stale_watches = [
            w for w in _active_watches(conn)
            if w["key_status"] != "active" or w["api_key"] in canceling
        ]

        verb = "marked" if args.apply else "would be marked"
        print(f"Non-free keys checked: {len(rows)}")
        print(f"  active with an active plan subscription: {len(keep)}")
        _report(f"no active subscription, {verb} canceled", to_cancel)
        print(f"  already not active, no active subscription: {len(already)}")
        _report("not active here but active in Stripe (not changed, look at these)", drifted)
        _report("could not check (not changed)", unknown)
        print(f"Active watches {verb} inactive: {len(stale_watches)}")
        _report("  on keys", list({w["api_key"] for w in stale_watches}))

        if args.apply:
            with conn:
                conn.executemany(
                    "UPDATE api_keys SET status = 'canceled' WHERE api_key = ? AND status = 'active'",
                    [(key,) for key in to_cancel],
                )
                if stale_watches:  # the watches table may not exist
                    conn.executemany(
                        "UPDATE watches SET status = 'inactive' WHERE id = ?",
                        [(w["id"],) for w in stale_watches],
                    )
    finally:
        conn.close()

    print("Done." if args.apply else "Dry run: nothing changed. Rerun with --apply to write.")
    return 2 if unknown else 0


if __name__ == "__main__":
    sys.exit(main())
