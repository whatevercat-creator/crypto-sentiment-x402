# Changelog

Changes to the `crypto-sentiment-x402-mcp` package (the MCP server). The
HTTP API itself isn't versioned here; dated "HTTP API" entries note
notable API changes.

## 0.2.0

**Migration: tools renamed, no aliases.** Update any client config or
prompt that names the old tools:

| 0.1.x | 0.2.0 |
|---|---|
| `crypto_sentiment` | `get_sentiment` |
| `get_hourly_history` | `get_sentiment_history` |
| `list_supported_coins` | `list_supported_coins` (unchanged) |
| `archive_coverage` | `get_archive_coverage` |
| `price_quote` | `get_call_price` |

Every tool name now follows `verb_noun`, and every description names only
tools that exist.

New tools:
- `get_market_sentiment` (free, no wallet): the market-wide sentiment index
  from `GET /market-sentiment`, with `cached`, `computed_at` and
  `next_refresh_at`. Refreshed at most hourly.
- `get_movers` ($0.02 per call): the coins whose sentiment rose or fell most
  over the last ~24 hours, from `GET /movers`. Optional `limit` (1 to 24,
  default 10). Same payment path and pre-signing check as the other paid
  tools: a 402 asking for more than $0.02, or for another payTo, network or
  asset, is refused before signing. A call that can rank no coin gets HTTP
  422 and isn't charged.

Spend cap:
- New `MAX_SPEND_USD_PER_SESSION`, on by default at $0.50 per server run.
  A paid call that would take the total over it is refused before signing,
  returned with `charged: false` and how to raise it. `0` turns the cap off;
  a value that isn't a non-negative number falls back to $0.50. Spending is
  counted when a payment is approved for signing.

Descriptions and titles:
- Titles are longer than the tool names and end in "(free)" or "(paid)".
- `get_sentiment` says each call fetches and scores the feeds live (no
  cache), so repeat calls can differ as news arrives.
- `get_sentiment_history`'s refusal list is one sentence instead of a
  bulleted block; every not-charged case is kept.
- Each paid tool's description says how its inputs are used (path or query
  parameter). The input schema descriptions are unchanged apart from the
  renamed tool in `get_sentiment_history`'s `symbol`.
- `get_call_price` lists all three paid prices.

Release files: version 0.2.0 in pyproject.toml and both places in
server.json; server.json gains `title`, `websiteUrl` and the
`MAX_SPEND_USD_PER_SESSION` variable. README, MCP.md and the API home page
list all 7 tools.

## 0.1.7

- Search words first. The MCP Registry description (server.json, 96 of
  100 characters) and the PyPI summary (pyproject.toml, which now opens
  with "Crypto news sentiment MCP server") say what the server is in buyer
  words.
- The `crypto_sentiment`, `get_hourly_history` and `list_supported_coins`
  descriptions open with what they return, followed by "Use when ...".
  `crypto_sentiment`'s description is shorter. The not-charged refusal
  wording is kept.
- `list_supported_coins` says the coin list is fixed per release: new coins
  come with a new package version, never while the server runs.
- README: a TODO for the Glama badge (no documented badge URL found yet).
- Version 0.1.7 in pyproject.toml and both places in server.json.

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
