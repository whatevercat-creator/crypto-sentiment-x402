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
   https://<your-deployment>/integrations/tradingview/<your-api-key>
   ```

   (Your API key goes in the URL path, not a header -- TradingView
   doesn't support custom headers on outbound webhooks.)

3. Set the alert **Message** to JSON that includes the ticker:

   ```json
   {"symbol": "{{ticker}}", "price": "{{close}}", "time": "{{time}}"}
   ```

   Plain-text messages work too (e.g. just `BTCUSD`) as a fallback, but
   JSON is more reliable since TradingView tickers vary
   (`BTCUSD`, `BTCUSDT`, `BINANCE:BTCUSDT`) -- this endpoint strips
   exchange prefixes and common quote-currency suffixes automatically.

4. When the alert fires, you'll get a message like:

   > 📈 TradingView alert fired for BTC -- current sentiment: bullish (0.41)

   through whichever channel your watch used.

If you haven't created a watch for that symbol first, the endpoint
returns a 404 telling you to -- it enriches alerts through channels you
control, it doesn't invent a delivery destination on its own.

Requires a Starter/Pro tier key (same as Zapier above, since this reuses
the same `watches` table and channel).

**Keep the subscription active.** The key in your webhook URL only works
while its Stripe subscription is `active` -- cancel or refund it and the
key flips to `canceled`, and every alert delivery after that gets a
`403 This subscription is not active.` (TradingView will show this as a
"webhook delivery failed" error on the alert.) If you're testing this
end-to-end, expect to either keep the subscription running afterward or
re-point the alert at a fresh active key once you're done testing.
