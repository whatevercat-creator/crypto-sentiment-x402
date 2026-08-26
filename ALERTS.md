# Sentiment-shift alerts

Watch a symbol; get pinged when its sentiment moves, instead of having to
poll `/sentiment` or `/v1/sentiment` yourself. Bundled into the existing
subscription tiers — no separate product or Stripe price:

| Tier | Watches allowed |
|---|---|
| Free | 0 (upgrade to use alerts) |
| Starter | 3 |
| Pro | 15 |

An alert fires when, since the last check, either the sentiment label
changes (e.g. neutral → bullish) or the compound score moves by at least
the watch's threshold (default `0.3`, override per-watch). The very first
check after creating a watch only records a baseline — nothing fires until
the check after that.

## How it works

A background loop inside the same running app (started in `app/main.py`'s
startup event, see `app/alerts.py`) polls every distinct watched symbol
every `ALERT_POLL_INTERVAL_SECONDS` (default 15 min), computes sentiment
the same way `/sentiment` does, and compares it to what was stored last
time.

**This only works correctly with a single running instance.** If you scale
this app to multiple instances behind a load balancer, either pin the
poller to one of them or move it out to a real scheduler/queue — as-is,
every instance would run its own independent loop and you'd get duplicate
alerts.

## Endpoints (require the same `X-API-Key` as `/v1/sentiment`)

```bash
# Watch BTC, get a plain webhook POST on shifts
curl -X POST $APP_BASE_URL/alerts/watch \
  -H "X-API-Key: csk_..." -H "Content-Type: application/json" \
  -d '{"symbol": "BTC", "channel_type": "webhook", "channel_target": "https://yourapp.com/hooks/sentiment"}'

# List your active watches
curl $APP_BASE_URL/alerts/watch -H "X-API-Key: csk_..."

# Stop one
curl -X DELETE $APP_BASE_URL/alerts/watch/1 -H "X-API-Key: csk_..."

# Recent deliveries (useful when a webhook seems to have gone quiet)
curl $APP_BASE_URL/alerts/history -H "X-API-Key: csk_..."
```

`threshold` is optional on `POST /alerts/watch` — omit it to use
`ALERT_THRESHOLD_DEFAULT`.

## Delivery channels

- **`webhook`** — `channel_target` is any URL. Gets a `POST` with a JSON
  body: `{symbol, old_label, new_label, old_compound, new_compound, timestamp}`.
  Works with Zapier, your own backend, anything that accepts a POST.
- **`discord`** — `channel_target` is a Discord webhook URL (channel
  settings → Integrations → Webhooks → New Webhook → Copy URL). Posts a
  formatted message directly into that channel.
- **`telegram`** — `channel_target` is the destination chat ID.
  Requires `TELEGRAM_BOT_TOKEN` to be set on the deployment (create a bot
  via [@BotFather](https://t.me/BotFather) on Telegram to get one). To find
  a chat ID: message your bot once, then visit
  `https://api.telegram.org/bot<token>/getUpdates` and read the chat id out
  of the response.

## Notes / what's deliberately left out of this first pass

- **No retry on failed delivery.** A failed webhook/Discord/Telegram send
  is logged in `alert_events` (check `/alerts/history`) but not retried —
  the next real sentiment shift is the next attempt. Add retry/backoff if
  delivery reliability matters more than simplicity for you.
- **No signature on outgoing webhooks.** If a receiver needs to verify a
  payload actually came from this service, add an HMAC signature header —
  not included yet.
- **Calendar-agnostic polling**, not per-customer scheduling — everyone's
  watches on the same symbol get checked together, which is efficient
  (one sentiment computation serves every watcher of that symbol) but
  means the check cadence is shared, not customizable per watch.
