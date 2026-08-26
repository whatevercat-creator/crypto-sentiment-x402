"""
MCP server wrapper for the Crypto Sentiment API (x402).

Lets any MCP client (Claude Desktop, Claude Code, etc.) call the pay-per-use
sentiment API as a tool. Every call pays for itself via x402, using a
Coinbase Developer Platform (CDP) wallet that belongs to whoever RUNS this
MCP server -- there is no free tier and no subsidized calls. If you're
handing this server to someone else to run, they fund the wallet, not you.

Required env vars (set these in the MCP client's env config, not in code):
  CDP_API_KEY_ID       - from https://portal.cdp.coinbase.com
  CDP_API_KEY_SECRET   - from https://portal.cdp.coinbase.com
  CDP_WALLET_NAME       - name for the wallet this server creates/reuses
                           (default: "crypto-sentiment-mcp-wallet")
  SENTIMENT_API_URL     - base URL of the deployed API
                           (default: https://crypto-sentiment-x402.onrender.com)
  X402_NETWORK           - "mainnet" (default) or "testnet"

The wallet must hold USDC (+ a little ETH for gas) on the chosen network
before calls succeed. Free testnet funds:
https://docs.base.org/tools/network-faucets

See MCP.md for how to register this with Claude Desktop / Claude Code.
"""

import os
from typing import Any

from cdp import CdpClient
from cdp.evm_local_account import EvmLocalAccount
from mcp.server.fastmcp import FastMCP
from x402 import x402Client
from x402.http.clients import x402HttpxClient
from x402.mechanisms.evm import EthAccountSigner
from x402.mechanisms.evm.exact import ExactEvmScheme

API_URL = os.environ.get(
    "SENTIMENT_API_URL", "https://crypto-sentiment-x402.onrender.com"
).rstrip("/")
NETWORK_MODE = os.environ.get("X402_NETWORK", "mainnet")
CAIP2_NETWORK = "eip155:8453" if NETWORK_MODE == "mainnet" else "eip155:84532"
WALLET_NAME = os.environ.get("CDP_WALLET_NAME", "crypto-sentiment-mcp-wallet")

mcp = FastMCP("crypto-sentiment")

_cdp: CdpClient | None = None
_payment_client: x402Client | None = None
_signer_address: str | None = None


async def _get_payment_client() -> x402Client:
    """Lazily create the caller's wallet + x402 payment client once, then reuse it."""
    global _cdp, _payment_client, _signer_address
    if _payment_client is not None:
        return _payment_client

    if not os.environ.get("CDP_API_KEY_ID") or not os.environ.get("CDP_API_KEY_SECRET"):
        raise RuntimeError(
            "CDP_API_KEY_ID and CDP_API_KEY_SECRET must be set in this MCP "
            "server's environment (get a free key at "
            "https://portal.cdp.coinbase.com). This wallet pays for every "
            "call out of its own funds -- fund it with USDC (+ a little ETH "
            "for gas) on the configured network before calling the tool."
        )

    _cdp = CdpClient()
    await _cdp.__aenter__()
    account = await _cdp.evm.get_or_create_account(name=WALLET_NAME)
    signer = EthAccountSigner(EvmLocalAccount(account))
    _signer_address = signer.address

    _payment_client = x402Client()
    _payment_client.register(CAIP2_NETWORK, ExactEvmScheme(signer))
    return _payment_client


@mcp.tool()
async def crypto_sentiment(symbol: str) -> dict[str, Any]:
    """
    Get real-time aggregate crypto sentiment for a ticker symbol (e.g. BTC, ETH, SOL).

    Pulls from Reddit, crypto news RSS (CoinDesk/Cointelegraph/Decrypt), and
    the Fear & Greed Index, scored with VADER plus a crypto slang lexicon.
    Returns a bullish/bearish/neutral label, a compound score, and a
    per-source breakdown.

    Each call pays this service's advertised x402 price in USDC on Base,
    charged to this MCP server's own configured wallet.
    """
    symbol = symbol.upper().strip()
    payment_client = await _get_payment_client()

    async with x402HttpxClient(payment_client) as http:
        response = await http.get(f"{API_URL}/sentiment/{symbol}")
        await response.aread()

    if response.status_code != 200:
        return {
            "error": f"API returned HTTP {response.status_code}",
            "detail": response.text,
            "paying_wallet": _signer_address,
            "hint": "A 402 here usually means the wallet above needs more "
            "USDC/ETH on the configured network.",
        }

    return response.json()


if __name__ == "__main__":
    mcp.run()
