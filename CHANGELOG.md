# Changelog

Changes to the `crypto-sentiment-x402-mcp` package (the MCP server). The
HTTP API itself isn't versioned here; dated "HTTP API" entries note
notable API changes.

## HTTP API, 2026-11-06 (deploy of 6819d87)

- Bazaar discovery: both paid routes' 402s now carry "Use when ..."
  descriptions (under 500 characters), `serviceName` "Crypto Sentiment API"
  and 5 tags; the /sentiment example shows the live window `72h-hl24-v2`.
  Price, payTo, network, asset and scheme unchanged (byte-identical
  `accepts[]`). Bazaar listings only pick this up after a payment settles
  on each route.
- OpenAPI: Paid / Subscription / Free tags, 200 response schemas for
  /sentiment and /history, and the 24-coin symbol enum on /history.
- /history `no_headlines_in_range` (404) now says "Not charged."
- New /robots.txt (paid routes stay crawlable) and /sitemap.xml; canonical
  and Open Graph tags on / and /archive; / links /history.
- One `free_signup` JSON log line per signup (email domain only).
- `scripts/deactivate_junk_free_keys.py`: dry run by default; `--apply`
  deactivates matched free-tier keys only.
- No change to the MCP package.

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
- The paid tools' no-wallet answer (no CDP keys set) now includes
  `charged: false`, like the other refusals.
- No change to prices, payTo, the payment guard or any API behavior.

## 0.1.5

- `x402` floored at 2.25.0. Paid tools refuse a 402 whose amount, payTo,
  network or asset doesn't match before signing anything.
