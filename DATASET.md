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

Included with Pro ($59/mo), and sold on its own as `TIERS["data"]` in
`app/billing.py`: $29/mo, bundled with a modest 3,000 `/v1/sentiment`
calls/month on top (so a data customer isn't locked out of the live
endpoint too). Change the price or call limit there the same way you would
Starter/Pro. Access is decided only by each tier's `dataset_access` flag,
so flipping that flag is all it takes to add or remove it from a tier.

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
  `dataset_access` tier (currently `pro` and `data`). Starter and Free
  keys get a 403. Query params:
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

## Hourly archive

Separately from the daily table above, `app/hourly.py` logs one reading per
symbol at the top of every UTC hour into its own `sentiment_hourly` table
(symbol, exact UTC `observed_at`, `average_compound`, `sample_size`,
`fear_greed_value`, `matcher`). All symbols are scored from one fetch of the
feeds per hour, so adding symbols adds no feed requests. `matcher` is
`whole_word` for readings made with the 2026-10-03 headline matcher and NULL
for earlier, substring-matched rows; `scripts/leadlag.py --whole-word-only`
analyzes only the former. `drivers` holds the top 3 headlines behind each
reading as JSON (NULL before 2026-10-03).

It feeds the lead/lag analysis and is sold three ways: `GET /archive` (free)
lists symbols, first reading, row counts and missing hours; `GET
/history/{symbol}?start=&end=` returns readings over x402 at
`X402_HISTORY_PRICE_USD` (default $0.05; default range 7 days, max 30); and
`/dataset/export` includes every hourly row for plans with dataset access
(`hourly_rows` in JSON, `table=hourly` for CSV). Readings are recorded live
and never backfilled. `HOURLY_SYMBOLS` (default `BTC,ETH`;
`render.yaml` sets `BTC,ETH,XRP,SOL`) picks the symbols and `HOURLY_ENABLED=0` turns it off. Missed hours are left as
gaps, and `fear_greed_value` only changes once a day since the index itself
is daily. About 1.8 MB/year for two symbols.
