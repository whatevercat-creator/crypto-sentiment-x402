# Stripe subscription tiers

The x402 pay-per-call endpoint (`/sentiment/{symbol}`) needs no setup and
keeps working regardless of anything below. This adds a second lane for
buyers who'd rather pay a predictable monthly price than hold USDC:
`/v1/sentiment/{symbol}`, gated by an `X-API-Key` header instead of x402.

Customer-facing terms (plan limits, cancellation, refunds) are at `GET /terms`, rendered by `render_terms` in `app/home.py`.

## Tiers

| Tier | Price | Calls/month | Get it |
|---|---|---|---|
| Free | $0 | 100 | `POST /billing/signup-free {"email": "..."}` |
| Starter | $15/mo | 3,000 (~$0.005/call) | `POST /billing/checkout/starter` |
| Pro | $59/mo | 15,000 (~$0.004/call), plus dataset export | `POST /billing/checkout/pro` |

Pro also includes the historical dataset export (`GET /dataset/export`),
which is otherwise sold on its own as the Data Access tier -- see
[DATASET.md](DATASET.md).

Both paid tiers are priced below the $0.01/call x402 rate as the incentive
to commit to a subscription. Adjust `TIERS` in `app/billing.py` if you want
different numbers — that dict is the only source of truth (no separate copy
of prices/limits to keep in sync).

Quota resets on the calendar month, not on the Stripe billing anchor date —
close enough for MVP; revisit if that mismatch matters to you.

## 1. Create the Stripe products

In the [Stripe dashboard](https://dashboard.stripe.com/test/products) (stay
in **test mode** until you're ready to charge real cards):

1. Create a product "Starter" with a **recurring monthly** price of $15 →
   copy its Price ID (`price_...`) into `STRIPE_PRICE_ID_STARTER`.
2. Create a product "Pro" with a recurring monthly price of $59 → copy its
   Price ID into `STRIPE_PRICE_ID_PRO`.
3. Create a product "Data Access" with a recurring monthly price of $29 →
   copy its Price ID into `STRIPE_PRICE_ID_DATA`.
4. Copy your test **Secret key** (Developers → API keys) into
   `STRIPE_SECRET_KEY`.

These price IDs are how the webhook recognises this service's plans. The
Stripe account also sells other things, so a checkout gets a key only if it is
in subscription mode and its prices are one of these three; everything else is
logged as ignored. A plan whose price ID isn't set can't be recognised, so its
buyers would get no key (the log shows `plan prices not configured`). Changing
a plan's price means updating its env var.

## 2. Set up the webhook

The webhook is what actually provisions an API key after checkout — without
it, `checkout.session.completed` never reaches this app and no key gets
created. Afterwards it keeps the key in step with the subscription: a key
works while the subscription is `active` or `trialing` and is turned off in
any other state, including `past_due`. It works again if the subscription
recovers, and follows a change of plan.

1. Developers → Webhooks → Add endpoint → URL:
   `{APP_BASE_URL}/billing/webhook`
   (locally, use the [Stripe CLI](https://docs.stripe.com/stripe-cli) —
   `stripe listen --forward-to localhost:8000/billing/webhook` — which
   prints a signing secret for you to use in step 2 below.)
2. Subscribe it to: `checkout.session.completed`,
   `customer.subscription.updated`, `customer.subscription.deleted`.
3. Copy the endpoint's **signing secret** into `STRIPE_WEBHOOK_SECRET`.

## 3. Set `APP_BASE_URL`

This has to be the real public URL Stripe will redirect back to after
checkout (e.g. `https://crypto-sentiment-x402.onrender.com`) — it's used to
build the `success_url`/`cancel_url` sent to Stripe. Wrong value = broken
redirect after payment.

## 4. Deploy

All five env vars above go into your Render service (or wherever you host
this) alongside the existing x402 ones. `render.yaml` already lists them as
`sync: false` placeholders, and adds a small persistent disk mounted at
`/data` so the sqlite file storing API keys survives redeploys — Render may
require a manual sync/redeploy from the dashboard the first time a disk is
added to an existing service, so double-check the disk actually attached
after your next deploy.

## 5. Test it end-to-end

```bash
# 1. Free tier, no Stripe needed:
curl -X POST $APP_BASE_URL/billing/signup-free -H 'Content-Type: application/json' -d '{"email":"you@yourdomain.com"}'
# -> {"api_key": "csk_...", ...}

curl $APP_BASE_URL/v1/sentiment/BTC -H "X-API-Key: csk_..."

# 2. Paid tier (use a Stripe test card, e.g. 4242 4242 4242 4242):
curl -X POST $APP_BASE_URL/billing/checkout/starter
# -> {"checkout_url": "https://checkout.stripe.com/..."}
# open that URL, complete checkout, land on /billing/success?session_id=...
# which shows the provisioned key once the webhook has landed (for 15 minutes).
```

## How to reissue a key

A paid key is shown on `/billing/success` only for 15 minutes after checkout.
A subscriber who loses it is told to email hi@forgealone.com from the address
they used at checkout. To replace their key:

1. Check the request came from the email on the subscription (Stripe
   dashboard → Customers). Don't send a key to any other address.
2. In the Render dashboard, open the service → **Shell** (the billing database
   is on its disk at `/data/billing.db`), and run:

   ```bash
   python -m app.reissue_key --email buyer@example.com
   # or by Stripe customer ID:
   python -m app.reissue_key --customer cus_ABC123
   ```

   It lists the matching keys by their prefix (first 8 characters) and asks
   before changing anything. If several active keys match, rerun with
   `--key-prefix csk_XXXX` (the prefix it printed) to pick one.
3. Reply to the subscriber with the new key the script prints. It's the
   only time the new key is shown: only its hash is stored.

The key is replaced in place: the old key stops working immediately, and the
plan, Stripe subscription, this month's usage, last-used time and alert
watches carry over.

## How keys are stored

Keys are stored hashed: `api_keys.api_key` (and `watches.api_key`) hold
`sha256:` + the hex SHA-256 of the key, never the key itself. Keys are 24
random bytes, so a plain hash is enough. `key_prefix` keeps the first 8
characters (e.g. `csk_AbC1`), which isn't enough to use the key, for support
and script output. A free key is shown once in the signup response; a paid
key on `/billing/success` for 15 minutes, from an in-memory cache (never
written to disk, emptied by a restart: the page then says the key was issued
and to email for a replacement).

Three columns are added at startup when missing:

- `key_prefix`: the first 8 characters.
- `last_used_at`: set when the key is issued, then on any request made with
  the active key (including one refused with 429 over quota), at most once a
  day outside `/v1/sentiment`.
- `ended_at`: for a paid key, when its subscription ended (deleted, or
  Stripe status `canceled` / `incomplete_expired`; not `past_due`, which can
  recover); for a free key, when it was switched off. Cleared if the key
  becomes active again.

### One-off: hash the keys issued before this release

Keys issued before the hashing release are still stored in plaintext. They
keep working (lookups accept either form) until this has run. In the Render
Shell, **after** the release is deployed (its startup adds the columns):

```bash
python scripts/backup_data.py                     # full backup first: the restore point
python scripts/hash_existing_keys.py              # dry run: counts and key prefixes only
python scripts/hash_existing_keys.py --apply      # only after reading the dry run
python scripts/hash_existing_keys.py              # again: should say plaintext: 0
```

`--apply` hashes every plaintext key and watch in one transaction, sets
`last_used_at` (and, on keys that aren't active, `ended_at`) to the
migration time where empty, then VACUUMs so no old plaintext stays in the
file. Existing keys' 12-month clock therefore starts at the migration date.
Afterwards take a fresh full backup and delete the older ones (they hold
plaintext keys). **Once applied, never deploy code older than the hashing
release:** it can't read hashed keys. Fix forward instead.

## Deleting expired keys (monthly, by hand)

The privacy policy keeps key data while it's in use and deletes it 12
months later: free keys 12 months after last use or after being switched
off, whichever comes first; paid keys 12 months after the subscription
ended. In the Render Shell:

```bash
python scripts/backup_data.py                     # full backup first
python scripts/purge_expired_keys.py              # dry run: counts and key prefixes only
python scripts/purge_expired_keys.py --apply      # only after reading the dry run
```

It deletes the matching keys with their watches and alert events, in one
transaction, then VACUUMs. It reports, and never deletes, paid keys that are
off but whose subscription hasn't ended (e.g. past_due), and watches whose
key is gone. It stops without changing anything until `hash_existing_keys.py
--apply` has run. Stripe's own records, the mailbox and backups (30-day
rule, see `scripts/backup_data.py`) aren't touched.

## Reconciling keys with Stripe

`app/maintenance/reconcile_keys.py` is a one-off for keys that drifted from
Stripe. Run it in the Render Shell, where the database and live Stripe key are:

```bash
python -m app.maintenance.reconcile_keys           # dry run: writes nothing
python -m app.maintenance.reconcile_keys --apply
```

For every non-free key it asks Stripe (read-only) for an active subscription
to one of this service's plans. Keys without one are marked canceled (and get
`ended_at`, unless they already have one), and watches of keys that aren't
active are marked inactive. It prints only counts and each key's prefix
(first 8 characters). A key it couldn't check is left alone, and
it never turns a canceled key back on: those are listed for you to look at.

## Customer billing portal

Subscribers manage their own subscription in Stripe's customer portal:
<https://billing.stripe.com/p/login/dRm28qcbjeNJaWn86f1Nu00>
(`STRIPE_PORTAL_LOGIN_URL` in `app/billing.py`; public, not a secret). The
home page links it under the plans ("Already subscribed?") and so does the
`/billing/success` page, below the key.

They enter their checkout email and Stripe emails them a one-time code.
There they can:

- **cancel**: takes effect at the end of the billing period, no partial
  refund;
- **update their card**;
- **see their invoices**.

They can't switch plans or change their email there.

Cancelling doesn't switch the key off straight away. Stripe first sends
`customer.subscription.updated` (still `active`, set to cancel at period
end), and `_sync_subscription` leaves the key working. When the period ends
Stripe sends `customer.subscription.deleted`, and `stripe_webhook` calls
`_sync_subscription(..., deleted=True)`, which sets the key's status to
`canceled` and stamps `ended_at` (the purge clock). Both are in
`app/billing.py`.

Email changes are left off because a lost key is reissued by the checkout
email: `python -m app.reissue_key --email` looks the key up by the email
stored at checkout (and "How to reissue a key" above checks the request came
from that address).

## Notes / what's deliberately left out of this first pass

- **No email delivery.** A free key is shown once, in the
  `/billing/signup-free` response. A paid key is shown on `/billing/success`
  only for 15 minutes after checkout; after that the link says the key was
  already issued and to email hi@forgealone.com from the checkout address.
  Add an email step (Stripe Checkout can collect the email, or send via any
  mail API) for self-service recovery — right now, a lost paid key is
  reissued by hand.
- **sqlite, not Postgres.** Fine for one instance at this scale; swap it out
  if you outgrow it or scale to multiple instances (the schema in
  `app/billing.py` is a few lines, easy to port).
- **Quota resets on calendar month**, not the subscriber's actual billing
  date — a customer who subscribes on the 20th doesn't get a clean 30-day
  window. Close enough for now.
