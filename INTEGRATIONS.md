# Zapier & TradingView integrations

Two ways to wire this API into tools you probably already use, both
built on top of the existing sentiment-shift alerts in [ALERTS.md](ALERTS.md)
rather than as separate products.

## Zapier

No new endpoint needed -- your existing alert watches already do this.
Zapier's **Webhooks by Zapier** app has a "Catch Hook" trigger that gives
you a URL; any `channel_type: "webhook"` watch you create already POSTs
a full JSON payload to any URL you give it, and Zapier's Catch Hook is
just another URL.

Steps:

1. In Zapier, create a Zap with trigger **Webhooks by Zapier -> Catch Hook**.
   Zapier gives you a URL like `https://hooks.zapier.com/hooks/catch/xxxx/yyyy/`.
2. Create a watch pointing at that URL:

   ```bash
   curl -X POST https://<your-deployment>/alerts/watch \
     -H "X-API-Key: <your-key>" \
     -H "Content-Type: application/json" \
     -d '{"symbol": "BTC", "channel_type": "webhook", "channel_target": "https://hooks.zapier.com/hooks/catch/xxxx/yyyy/", "threshold": 0.3}'
   ```

3. Back in Zapier, trigger a test alert (or wait for a real sentiment
   shift) to pull in a sample payload, then build whatever action you
   want from there -- post to Slack, add a row to Google Sheets, send a
   text via Twilio, log to Airtable, etc.

The payload Zapier receives on each shift:

```json
{
  "symbol": "BTC",
  "old_label": "neutral",
  "new_label": "bullish",
  "old_compound": 0.05,
  "new_compound": 0.41,
  "timestamp": "2026-08-28T19:00:00+00:00"
}
```

Requires a Starter/Pro tier key (same `alert_limit` as any other watch --
see ALERTS.md).

## TradingView

TradingView alerts can POST to a webhook URL when they fire, but they
can't attach custom headers, and Pine Script itself can't make outbound
HTTP calls -- so pulling this API's data *live into a chart/indicator*
isn't possible. What *is* possible, and what this integration does: when
a TradingView alert fires, it hits our endpoint, we look up the current
sentiment for that symbol, and relay a combined "price alert + sentiment"
message through the same webhook/Discord/Telegram channel you've already
set up via `/alerts/watch`.

Steps:

1. Create a watch for the symbol you want (this is where the delivery
   channel comes from -- webhook, Discord, or Telegram):

   ```bash
   curl -X POST https://<your-deployment>/alerts/watch \
     -H "X-API-Key: <your-key>" \
     -H "Content-Type: application/json" \
     -d '{"symbol": "BTC", "channel_type": "discord", "channel_target": "<your discord webhook url>"}'
   ```

2. In TradingView, create an alert on whatever condition you want (price
   cross, indicator signal, etc). In the alert's **Notifications** tab,
   enable **Webhook URL** and set it to:

   ```
   https://<your-deployment>/integrations/tradingview
   ```

   No key in the URL: URLs end up in request logs, so the key goes in
   the alert message instead (next step). TradingView doesn't support
   custom headers on outbound webhooks, so an `X-API-Key` header isn't
   an option.

3. Set the alert **Message** to JSON with your API key and the ticker:

   ```json
   {"api_key": "<your-api-key>", "symbol": "{{ticker}}", "price": "{{close}}", "time": "{{time}}"}
   ```

   The message has to be JSON, since it carries the key. The `api_key`
   field is removed before anything is relayed, so your key never
   appears in your webhook, Discord or Telegram messages. TradingView
   tickers vary (`BTCUSD`, `BTCUSDT`, `BINANCE:BTCUSDT`): this endpoint
   strips exchange prefixes and common quote-currency suffixes
   automatically. `ticker` works as the field name too.

   Treat the alert message like a password: anyone who can read your
   TradingView alerts can see the key.

4. When the alert fires, you'll get a message like:

   > 📈 TradingView alert fired for BTC -- current sentiment: bullish (0.41)

   through whichever channel your watch used.

If you haven't created a watch for that symbol first, the endpoint
returns a 404 telling you to -- it enriches alerts through channels you
control, it doesn't invent a delivery destination on its own. A message
without an `api_key` gets a 401, and one without a `symbol` (or
`ticker`) a 400.

Requires a Starter/Pro tier key (same as Zapier above, since this reuses
the same `watches` table and channel).

**Keep the subscription active.** The key in your alert message only works
while its Stripe subscription is `active` -- cancel or refund it and the
key flips to `canceled`, and every alert delivery after that gets a
`403 This subscription is not active.` (TradingView will show this as a
"webhook delivery failed" error on the alert.) If you're testing this
end-to-end, expect to either keep the subscription running afterward or
re-point the alert at a fresh active key once you're done testing.

### Deprecated: the key in the URL path

Older setups used `https://<your-deployment>/integrations/tradingview/<your-api-key>`
as the Webhook URL, with the key in the path. That still works for now,
but a URL path ends up in request logs, so:

- its responses carry a `"deprecated"` field pointing to the form above;
- each call logs one line, `{"event": "tradingview_path_key", "key_prefix": "<first 8 characters>"}`,
  never the full key, so we can see whether anyone still uses it;
- the access log shows the path as `/integrations/tradingview/[redacted]`.

**It will be removed after 60 days in which the logs show no
`tradingview_path_key` line.** To switch: change the alert's Webhook URL
to `https://<your-deployment>/integrations/tradingview` and add
`"api_key": "<your-api-key>"` to its message. If your key was ever in a
Webhook URL, you can ask for a replacement key: email hi@forgealone.com
from the address you used at checkout.
