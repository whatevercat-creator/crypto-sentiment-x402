# MCP server

`mcp_server.py` exposes the sentiment API as an MCP tool (`crypto_sentiment`)
so Claude Desktop, Claude Code, or any other MCP client can call it directly
as part of an agent's normal tool use — no separate HTTP client code needed
on the caller's side.

**Payment model:** the MCP server itself holds a wallet (via Coinbase
Developer Platform) and pays the API's x402 price out of that wallet for
every call. There's no free tier — whoever runs this MCP server funds the
wallet it uses. If you're publishing this for others to run themselves,
each person funds their own.

## 1. Install

```bash
pip install -r requirements.txt -r requirements-mcp.txt
```

## 2. Create a CDP API key and fund a wallet

1. Free account + API key at https://portal.cdp.coinbase.com
2. Pick a network:
   - `testnet` — free Base Sepolia USDC from
     https://docs.base.org/tools/network-faucets, good for trying this out
     with no real money.
   - `mainnet` — real USDC on Base; the wallet needs actual USDC (plus a
     little ETH for gas) before calls will succeed.
3. The wallet is created automatically the first time the server runs
   (named by `CDP_WALLET_NAME`, default `crypto-sentiment-mcp-wallet`) — run
   it once locally and check the logs/tool output for the address it
   prints, then send that address USDC (+ ETH on mainnet) before relying on
   it inside an agent.

## 3. Register it with an MCP client

**Claude Desktop** — add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "crypto-sentiment": {
      "command": "python",
      "args": ["/absolute/path/to/crypto-sentiment-x402/mcp_server.py"],
      "env": {
        "CDP_API_KEY_ID": "your-key-id",
        "CDP_API_KEY_SECRET": "your-key-secret",
        "CDP_WALLET_NAME": "crypto-sentiment-mcp-wallet",
        "SENTIMENT_API_URL": "https://crypto-sentiment-x402.onrender.com",
        "X402_NETWORK": "testnet"
      }
    }
  }
}
```

**Claude Code** — same shape, either via `claude mcp add` or a project
`.mcp.json` with the same `mcpServers` block.

Any other MCP-compatible client works the same way: it just needs a command
to launch `mcp_server.py` with those env vars set.

## 4. Use it

Once registered, an agent can call the `crypto_sentiment` tool with a
`symbol` argument (e.g. `BTC`) like any other tool — the payment happens
transparently inside the call. If a call returns an `error` field, it's
almost always the wallet needing more funds; the response includes the
paying wallet's address to check.

## Notes

- `SENTIMENT_API_URL` defaults to the hosted deployment; point it at
  `http://localhost:8000` to test against a local `uvicorn app.main:app`
  instead.
- This is a separate process/wallet from `client.py` (a one-off test
  script) — both pay the same API, just for different purposes.
