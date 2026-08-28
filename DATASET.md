# Historical dataset export

A second product built on the same sentiment engine: instead of a live
reading, this sells access to the history of readings over time, so
someone can chart or backtest against sentiment rather than only ever
seeing "right now."

**Honesty check first:** there is no backfilled history. The dataset
starts accumulating from whenever the snapshot loop first runs in your
deployment and grows by one row per tracked symbol per day after that.
`GET /dataset/info` always tells a prospective buyer the true first/last
dates and row count -- don't market this as having deep history it
doesn't have.

## How it works

A background loop (started in `app/main.py`, same pattern as the alert
poller in `app/alerts.py`) wakes up periodically and, for each symbol in
`SNAPSHOT_SYMBOLS` (default: everything in `app/coins.py`), records one
row -- label, compound score, sample size, Fear & Greed value -- if it
hasn't already recorded one for that symbol today. Restarting the app
doesn't create duplicate rows for the same day.

**Same single-instance caveat as alerts** (see ALERTS.md) -- this assumes
one running instance.

## Pricing

Sold as its own tier, `TIERS["data"]` in `app/billing.py`: $29/mo,
bundled with a modest 3,000 `/v1/sentiment` calls/month on top (so a data
customer isn't locked out of the live endpoint too). Change the price or
call limit there the same way you would Starter/Pro.

## Setup

This reuses your existing Stripe account, secret key, and webhook
endpoint -- nothing new to configure there beyond one more product/price:

1. Create a "Data Access" product in Stripe with a $29/mo recurring price
   (same steps as BILLING.md's Starter/Pro setup).
2. Set `STRIPE_PRICE_ID_DATA` to that price's ID.
3. That's it -- your existing webhook already listens for
   `checkout.session.completed` and provisions a key regardless of which
   tier's price was purchased.

## Endpoints

- `GET /dataset/info` -- public, no auth. Shows tracked symbols, first/last
  snapshot dates, and total row count. Good for a pricing/landing page to
  link to so prospective buyers see real numbers before paying.
- `GET /dataset/export` -- requires `X-API-Key` from a key on a
  `dataset_access` tier (currently just `data`, but Pro could be upgraded
  to include it by flipping one flag in `TIERS`). Query params:
  `format=csv|json` (default csv), `symbol=BTC` (optional filter),
  `since=YYYY-MM-DD` (optional filter). Returns everything matching, no
  pagination yet -- fine at current scale, revisit if rows get large.

```bash
curl "$APP_BASE_URL/dataset/export?format=csv" -H "X-API-Key: csk_..." -o history.csv
```

## Notes / what's deliberately left out of this first pass

- **No pagination** on `/dataset/export` -- at (symbols × days) row counts
  this stays small for a long time, but revisit if `SNAPSHOT_SYMBOLS` grows
  a lot or this runs for years.
- **No listing on an external marketplace yet** (Kaggle, AWS Data
  Exchange, etc.) -- this ships as a self-serve product on your own API
  first. Those are heavier integrations (seller registration, listing
  review) worth doing once there's a track record of the export actually
  being useful to someone.
