# Stripe subscription tiers

The x402 pay-per-call endpoint (`/sentiment/{symbol}`) needs no setup and
keeps working regardless of anything below. This adds a second lane for
buyers who'd rather pay a predictable monthly price than hold USDC:
`/v1/sentiment/{symbol}`, gated by an `X-API-Key` header instead of x402.

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
3. Copy your test **Secret key** (Developers → API keys) into
   `STRIPE_SECRET_KEY`.

## 2. Set up the webhook

The webhook is what actually provisions an API key after checkout — without
it, `checkout.session.completed` never reaches this app and no key gets
created.

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
curl -X POST $APP_BASE_URL/billing/signup-free -H 'Content-Type: application/json' -d '{"email":"you@example.com"}'
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

   It lists the matching keys (masked) and asks before changing anything. If
   several active keys match, rerun with `--key-prefix csk_XXXX` to pick one.
3. Reply to the subscriber with the new key the script prints.

The key is replaced in place: the old key stops working immediately, and the
plan, Stripe subscription, this month's usage and alert watches carry over.

## Notes / what's deliberately left out of this first pass

- **No email delivery.** A free key is shown once, in the
  `/billing/signup-free` response. A paid key is shown on `/billing/success`
  only for 15 minutes after checkout; after that the link says the key was
  already issued and to email hi@forgealone.com from the checkout address.
  Add an email step (Stripe Checkout can collect the email, or send via any
  mail API) for self-service recovery — right now, a lost paid key is
  reissued by hand.
- **No customer self-service portal** (cancel/upgrade/see usage). Stripe's
  [Billing Portal](https://docs.stripe.com/customer-management) is a fast
  way to add one — a few lines against `stripe.billingPortal.Session`.
- **sqlite, not Postgres.** Fine for one instance at this scale; swap it out
  if you outgrow it or scale to multiple instances (the schema in
  `app/billing.py` is a few lines, easy to port).
- **Quota resets on calendar month**, not the subscriber's actual billing
  date — a customer who subscribes on the 20th doesn't get a clean 30-day
  window. Close enough for now.
