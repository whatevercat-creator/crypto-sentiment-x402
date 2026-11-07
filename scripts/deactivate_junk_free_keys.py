"""
One-off: deactivate free-tier API keys signed up with obvious test/junk
emails. Run it from the Render Shell, where the billing database is:

    python scripts/deactivate_junk_free_keys.py            # dry run, changes nothing
    python scripts/deactivate_junk_free_keys.py --apply    # only after reading the dry run

DRY RUN is the default: it opens the database read-only and prints the rule
list, counts per rule, and for each matched key its first 8 characters, the
email's domain and the rule it matched. Never full keys or full emails.

--apply sets status = 'deactivated' on the matched keys (deactivate, never
delete). Only rows with tier = 'free' AND status = 'active' are ever read or
changed: paid tiers are never touched. A deactivated key gets HTTP 403
"This subscription is not active." and can't sign up again under the same key.

Database: $BILLING_DB_PATH (default "billing.db"), or --db PATH. The file must
already exist; this script never creates one.
"""

import argparse
import os
import re
import sqlite3
import sys
from collections import Counter

# (rule name, plain-language description). Printed at the top of every run.
RULES = (
    ("invalid_email", "no usable address: empty, no '@', or nothing after it"),
    ("reserved_domain", "reserved/documentation domains: example.com, example.org, example.net "
     "(and their subdomains), and the .test, .example, .invalid and .localhost TLDs"),
    ("disposable_domain", "known throwaway-inbox services (mailinator-style), e.g. mailinator.com, "
     "yopmail.com, guerrillamail.com, 10minutemail.com, sharklasers.com"),
    ("test_local_part", "the part before '@' is a placeholder: test, testing, tester, asdf, "
     "qwerty, foo, bar, foobar, abc, fake, dummy, sample, null, none, noreply, xxx, aaa "
     "(optionally followed by digits)"),
)

RESERVED_DOMAINS = ("example.com", "example.org", "example.net")
RESERVED_TLDS = ("test", "example", "invalid", "localhost")

DISPOSABLE_DOMAINS = frozenset({
    "mailinator.com", "mailinator.net", "mailinator.org", "mailinator2.com",
    "yopmail.com", "yopmail.net", "yopmail.fr",
    "guerrillamail.com", "guerrillamail.net", "guerrillamail.org", "guerrillamail.biz",
    "guerrillamailblock.com", "sharklasers.com", "grr.la", "spam4.me",
    "10minutemail.com", "10minutemail.net", "20minutemail.com",
    "tempmail.com", "temp-mail.org", "temp-mail.io", "tempmail.dev", "tempmailo.com",
    "trashmail.com", "trashmail.de", "throwawaymail.com", "dispostable.com",
    "maildrop.cc", "getnada.com", "nada.email", "mailnesia.com", "fakeinbox.com",
    "mintemail.com", "emailondeck.com", "mohmal.com", "discard.email",
    "mailcatch.com", "spamgourmet.com", "mytemp.email", "burnermail.io",
})

TEST_LOCAL_PART = re.compile(
    r"^(test|testing|tester|asdf|qwerty|foo|bar|foobar|abc|fake|dummy|sample|null|none|"
    r"noreply|no-reply|x{3,}|a{3,})\d*$"
)

PREFIX_LEN = 8
DEACTIVATED = "deactivated"


def split_email(email):
    """(local part, domain), both lowercased; domain is '' when there isn't one."""
    email = (email or "").strip().lower()
    local, at, domain = email.rpartition("@")
    if not at:
        return email, ""
    return local, domain.rstrip(".")


def match_rule(email):
    """The first rule `email` matches, or None for an address that looks real."""
    local, domain = split_email(email)
    if not local or not domain:
        return "invalid_email"
    if any(domain == d or domain.endswith("." + d) for d in RESERVED_DOMAINS):
        return "reserved_domain"
    if domain.rsplit(".", 1)[-1] in RESERVED_TLDS:
        return "reserved_domain"
    if domain in DISPOSABLE_DOMAINS or any(domain.endswith("." + d) for d in DISPOSABLE_DOMAINS):
        return "disposable_domain"
    if TEST_LOCAL_PART.match(local.split("+", 1)[0]):
        return "test_local_part"
    return None


def find_junk(conn):
    """[(api_key, domain, rule)] for active free-tier keys whose email matches a rule."""
    rows = conn.execute(
        "SELECT api_key, email FROM api_keys WHERE tier = 'free' AND status = 'active' "
        "ORDER BY created_at, api_key"
    ).fetchall()
    out = []
    for api_key, email in rows:
        rule = match_rule(email)
        if rule:
            out.append((api_key, split_email(email)[1] or "(none)", rule))
    return out, len(rows)


def _connect(path, apply):
    if not os.path.isfile(path):
        sys.exit(f"Database not found: {path} (set BILLING_DB_PATH or pass --db). Nothing changed.")
    mode = "rw" if apply else "ro"
    return sqlite3.connect(f"file:{path}?mode={mode}", uri=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=os.environ.get("BILLING_DB_PATH", "billing.db"))
    parser.add_argument("--apply", action="store_true", help="deactivate the matched keys (default: dry run)")
    args = parser.parse_args(argv)

    print("MODE: APPLY (matched keys will be deactivated)" if args.apply else "MODE: DRY RUN (nothing will be changed)")
    print("Rules (first match wins, checked in this order):")
    for name, text in RULES:
        print(f"  - {name}: {text}")
    print("Scope: tier = 'free' AND status = 'active' only. Paid tiers are never read or changed.")
    print()

    conn = _connect(args.db, args.apply)
    try:
        junk, active_free = find_junk(conn)
        counts = Counter(rule for _, _, rule in junk)
        print(f"Active free keys: {active_free}")
        print(f"Matched: {len(junk)}")
        for name, _ in RULES:
            print(f"  {name}: {counts.get(name, 0)}")
        print()
        if junk:
            print(f"{'key prefix':<12} {'rule':<18} email domain")
            for api_key, domain, rule in junk:
                print(f"{api_key[:PREFIX_LEN]:<12} {rule:<18} {domain}")
            print()

        if not args.apply:
            print("DRY RUN: nothing was changed. Re-run with --apply to deactivate these keys.")
            return 0

        with conn:
            changed = 0
            for api_key, _, _ in junk:
                changed += conn.execute(
                    "UPDATE api_keys SET status = ? "
                    "WHERE api_key = ? AND tier = 'free' AND status = 'active'",
                    (DEACTIVATED, api_key),
                ).rowcount
        print(f"APPLIED: deactivated {changed} free key(s). Nothing was deleted.")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
