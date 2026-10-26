# Changelog

Changes to the `crypto-sentiment-x402-mcp` package (the MCP server). The
HTTP API itself isn't versioned here.

## 0.1.6

- Tool descriptions rewritten to match the API's actual behavior: valid
  symbols (exact uppercase tickers from `list_supported_coins`), every
  not-charged refusal (unsupported symbol, "insufficient recent news",
  invalid range, unlogged symbol, `no_readings_in_range` and the new
  `no_headlines_in_range`), how `get_hourly_history`'s start and end
  interact, when not to call `price_quote`, and `archive_coverage`'s new
  `coverage_7d_pct`.
- MCP tool annotations and titles on every tool. Free tools
  (`list_supported_coins`, `archive_coverage`, `price_quote`) are read-only
  and idempotent; paid tools (`crypto_sentiment`, `get_hourly_history`) are
  neither, since each call charges. None are destructive; all but
  `list_supported_coins` (a local list, no network call) are open-world.
- `get_hourly_history`'s `symbol` argument now has a schema description.
- No change to prices, payTo, the payment guard or any API behavior.

## 0.1.5

- `x402` floored at 2.25.0. Paid tools refuse a 402 whose amount, payTo,
  network or asset doesn't match before signing anything.
