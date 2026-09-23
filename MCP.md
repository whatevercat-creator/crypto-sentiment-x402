# MCP server

mcp-name: io.github.whatevercat-creator/crypto-sentiment-x402

The `crypto_sentiment_x402_mcp` package exposes the sentiment API as an MCP
tool (`crypto_sentiment`) so Claude Desktop, Claude Code, or any other MCP
client can call it directly as part of an agent's normal tool use — no
separate HTTP client code needed on the caller's side.

**Payment model:** the MCP server itself holds a wallet (via Coinbase
Developer Platform) and pays the API's x402 price out of that wallet for
every call, entirely inside your own local process — invisible to the MCP
protocol layer, so it works in Claude Desktop (or any other stock MCP
client) exactly like a normal, unpaid MCP server would. There's no free
tier — whoever runs this MCP server funds the wallet it uses. If you're
handing it to someone else to run, they fund their own wallet, not you.

## 1. Install

```bash
pip install crypto-sentiment-x402-mcp
```

(`uvx crypto-sentiment-x402-mcp` works too, and needs no separate install
step.)

**Don't install this into the same environment as the FastAPI service**
(`requirements.txt`). `mcp>=2` pulls in a much newer `starlette` than
FastAPI 0.115.0 tolerates (`starlette<0.39.0,>=0.37.2`) — mixing the two in
one venv breaks `app/main.py` at import time (`Router.__init__() got an
unexpected keyword argument 'on_startup'`). If you're hacking on this repo
and want both, give the MCP package its own venv:

```bash
python3 -m venv .venv-mcp && source .venv-mcp/bin/activate
pip install -r requirements-mcp.txt cdp-sdk x402
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
      "command": "crypto-sentiment-x402-mcp",
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

(That assumes `pip install crypto-sentiment-x402-mcp` put the console script
on `PATH` for whatever Python Claude Desktop launches with. If you'd rather
not rely on `PATH`, point `command` at the interpreter and
`args: ["-m", "crypto_sentiment_x402_mcp"]`, or, from a repo clone, at
`mcp_server.py` directly, same as before — both still work.)

**Claude Code** — same shape, either via `claude mcp add` or a project
`.mcp.json` with the same `mcpServers` block.

Any other MCP-compatible client works the same way: it just needs a command
to launch the server with those env vars set.

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

## License

MIT (see `LICENSE-MCP`) — this package only. The rest of this repository,
including the hosted API in `app/`, remains All Rights Reserved under the
root `LICENSE`.
