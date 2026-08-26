# Crypto Sentiment API (x402)

Pay-per-call crypto sentiment API for AI agents. No API keys, no
subscriptions — agents discover it, get an HTTP 402, pay in USDC on Base,
and get the data.

**Sentiment sources (all free, no keys required):**
- Reddit (r/CryptoCurrency, r/Bitcoin, r/CryptoMarkets) — public JSON endpoints
- Crypto news RSS — CoinDesk, Cointelegraph, Decrypt
- Fear & Greed Index — alternative.me

**Scoring:** VADER sentiment analysis, extended with a crypto slang lexicon
(moon, rekt, rug, hodl, bullish/bearish, etc.) so slang isn't scored as neutral.

---

## 1. Get a Base wallet address

You need an address to receive USDC payments. Any standard Ethereum-style
wallet works on Base (e.g. Coinbase Wallet, MetaMask configured for Base).
You do **not** need to give this app your private key — only the public
address, to receive funds.

## 2. Run it locally (testnet — free, no real money)

```bash
cd crypto-sentiment-x402
cp .env.example .env
# edit .env and set PAY_TO_ADDRESS to your wallet address
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Visit `http://localhost:8000` — you'll see the API description.
Calling `http://localhost:8000/sentiment/BTC` without payment returns an
HTTP 402 with payment instructions, exactly what an agent's x402 client
would see and act on automatically.

To actually test a paid call, use the official x402 test client (needs
Base Sepolia testnet USDC — free from the
[Base Sepolia faucet](https://docs.base.org/tools/network-faucets)):

```bash
pip install x402
python -m x402.examples.client --url http://localhost:8000/sentiment/BTC
```

## 3. Deploy (simplest path: Render.com)

1. Push this folder to a new GitHub repo.
2. Go to [render.com](https://render.com) → New → Blueprint → connect the repo.
   Render will read `render.yaml` automatically.
3. When prompted, set the `PAY_TO_ADDRESS` environment variable to your
   wallet address.
4. Deploy. Render builds the `Dockerfile` and gives you a public HTTPS
   URL like `https://crypto-sentiment-x402.onrender.com`.

That's it — testnet mode is on by default, so nothing costs real money
until you flip `X402_NETWORK` to `mainnet` (step 4 below).

## 4. Go live with real USDC payments

1. Sign up for a free [Coinbase Developer Platform](https://portal.cdp.coinbase.com)
   account and create an API key — this is required for the mainnet
   facilitator (production-grade payment verification/settlement).
2. In Render, set:
   - `X402_NETWORK=mainnet`
   - `CDP_API_KEY_ID=...`
   - `CDP_API_KEY_SECRET=...`
   - `PAY_TO_ADDRESS` = your **mainnet** Base address (double-check it's
     not a testnet-only address)
3. Redeploy. Calls now settle real USDC on Base mainnet.

Start with a low price (`X402_PRICE_USD`, default `$0.01`) while you
validate everything works end-to-end.

## 5. List it so AI agents can find and pay you

This is the **x402 Bazaar** — Coinbase's discovery layer, essentially a
search engine for agents looking for x402 services.

- If you're using the **CDP facilitator** (i.e. you're on mainnet per step
  4), your service becomes automatically discoverable in the Bazaar once
  you process your first real payment through it — no separate signup.
- To improve how well agents can find and understand it, keep the
  `description` field in `app/main.py`'s `RouteConfig` clear and specific
  (already set) — that text is what shows up in Bazaar search results.
- You can browse the current Bazaar listing yourself at
  `https://api.cdp.coinbase.com/platform/v2/x402/discovery/resources`
  (public, no key needed) to confirm your service appears after going live.
- Optional extra distribution: submit your API to
  [x402bazaar.org](https://www.x402bazaar.org/), a community-run directory
  of x402 services, separate from Coinbase's own Bazaar.

## MCP server (Claude Desktop / Claude Code)

Prefer agents calling this over MCP instead of raw HTTP? `mcp_server.py`
wraps the same API as an MCP tool (`crypto_sentiment`) -- the server pays
for each call from its own CDP-managed wallet, so callers never touch
crypto directly. See [MCP.md](MCP.md) for setup.

## API reference

| Endpoint | Price | Description |
|---|---|---|
| `GET /` | Free | Service info |
| `GET /health` | Free | Health check |
| `GET /sentiment/{symbol}` | $0.01 (configurable) | Aggregate sentiment for a symbol, e.g. `/sentiment/BTC` |

Example paid response:

```json
{
  "symbol": "BTC",
  "name": "Bitcoin",
  "overall_sentiment": {
    "sample_size": 42,
    "average_compound": 0.21,
    "label": "bullish",
    "positive_pct": 55.0,
    "negative_pct": 15.0,
    "neutral_pct": 30.0
  },
  "breakdown": {
    "reddit": { "...": "..." },
    "news": { "...": "..." },
    "fear_greed_index": { "value": 62, "classification": "Greed" }
  },
  "sources": ["reddit.com (...)", "coindesk.com RSS", "..."]
}
```

## Notes and limitations

- Reddit's public JSON endpoints are unauthenticated and can be rate-limited
  or blocked under heavy load. If you outgrow them, swap `app/sources/reddit.py`
  for a free Reddit "script" app + [PRAW](https://praw.readthedocs.io/)
  (still free, just requires registering an app).
- Coin coverage: `app/coins.py` has a starter list of ~15 symbols. Add more
  as needed — unmapped symbols still work, just with slightly less accurate
  news matching (falls back to matching on the symbol itself).
- Only supports the `exact` payment scheme on Base/EVM. The x402 SDK also
  supports Solana (`ExactSvmServerScheme`) if you want to accept SOL-based
  USDC too — see the commented-out import in `app/main.py`'s dependencies.
- Add more paid endpoints (e.g. historical sentiment, multi-symbol batch
  queries) by adding entries to the `routes` dict in `app/main.py`.
