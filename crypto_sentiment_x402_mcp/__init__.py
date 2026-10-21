"""
MCP server for the Crypto Sentiment API (x402).

Lets any MCP client (Claude Desktop, Claude Code, etc.) call the pay-per-use
sentiment API as a tool. The sentiment reading (crypto_sentiment) pays for
itself via x402, using a Coinbase Developer Platform (CDP) wallet that
belongs to whoever RUNS this MCP server -- the paid reading has no free tier
and no subsidized calls. If you're handing this server to someone else to
run, they fund the wallet, not you.

Three info tools are free and need no wallet: list_supported_coins,
archive_coverage and price_quote. The server starts without any CDP
variables; only the paid tools (crypto_sentiment and get_hourly_history)
need them.

Env vars for the paid tools (set these in the MCP client's env config,
not in code):
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
from importlib.metadata import PackageNotFoundError, version
from typing import Annotated, Any, Literal

import base64
import binascii
import json

import httpx
from cdp import CdpClient
from cdp.evm_local_account import EvmLocalAccount
from mcp.server.mcpserver import MCPServer
from pydantic import Field
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

try:
    __version__ = version("crypto-sentiment-x402-mcp")
except PackageNotFoundError:  # running from a clone without installing
    __version__ = "0.0.0+unknown"

mcp = MCPServer("crypto-sentiment", version=__version__)

# Same symbols, names and order as COIN_NAMES in app/coins.py (the package
# can't import app; tests/test_mcp_free_tools.py checks they match).
SUPPORTED_COINS = {
    "BTC": "Bitcoin",
    "ETH": "Ethereum",
    "SOL": "Solana",
    "XRP": "Ripple",
    "DOGE": "Dogecoin",
    "ADA": "Cardano",
    "AVAX": "Avalanche",
    "LINK": "Chainlink",
    "MATIC": "Polygon",
    "DOT": "Polkadot",
    "SHIB": "Shiba Inu",
    "LTC": "Litecoin",
    "BNB": "BNB",
    "USDC": "USD Coin",
    "USDT": "Tether",
    "NEAR": "NEAR Protocol",
    "HYPE": "Hyperliquid",
    "QNT": "Quant",
    "AAVE": "Aave",
    "TAO": "Bittensor",
    "PEPE": "Pepe",
    "PUMP": "Pump.fun",
    "ENA": "Ethena",
    "STRK": "Starknet",
}

# Typed as an enum in the tool schemas so clients only offer these. Keep in
# the same order as SUPPORTED_COINS (tests check).
CoinSymbol = Literal[
    "BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "AVAX", "LINK",
    "MATIC", "DOT", "SHIB", "LTC", "BNB", "USDC", "USDT", "NEAR",
    "HYPE", "QNT", "AAVE", "TAO", "PEPE", "PUMP", "ENA", "STRK",
]
SymbolArg = Annotated[
    CoinSymbol,
    Field(
        description="Uppercase crypto ticker, one of the 24 from "
        "list_supported_coins (e.g. BTC). Stock tickers and other symbols "
        "are refused and not charged."
    ),
]
TimeArg = Annotated[
    str | None,
    Field(
        description="ISO 8601 timestamp, e.g. 2026-10-13T00:00:00Z (UTC if "
        "no offset). start defaults to end minus 7 days; end is exclusive "
        "and defaults to now. At most 30 days per call."
    ),
]

FREE_TOOLS = ("list_supported_coins", "archive_coverage", "price_quote")

# USDC uses 6 decimals on Base and Base Sepolia; 402 amounts are base units.
USDC_DECIMALS = 6

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


def _no_wallet(what: str) -> dict[str, Any] | None:
    """The friendly answer for a paid tool when no CDP keys are set, else None."""
    if os.environ.get("CDP_API_KEY_ID") and os.environ.get("CDP_API_KEY_SECRET"):
        return None
    return {
        "error": "no wallet configured",
        "detail": f"The paid {what} needs a funded CDP wallet: set "
        "CDP_API_KEY_ID and CDP_API_KEY_SECRET in this MCP server's "
        "environment (get a key at https://portal.cdp.coinbase.com) and "
        "fund the wallet with USDC (+ a little ETH for gas) on Base.",
        "free_tools": list(FREE_TOOLS),
        "hint": "list_supported_coins, archive_coverage and price_quote "
        "are free and need no wallet.",
    }


async def _paid_get(url: str, params: dict[str, str] | None = None) -> dict[str, Any]:
    """GET a paid endpoint through the x402 payment client."""
    payment_client = await _get_payment_client()

    async with x402HttpxClient(payment_client) as http:
        response = await http.get(url, params=params)
        await response.aread()

    if response.status_code == 402:
        return {
            "error": "API returned HTTP 402",
            "detail": response.text,
            "paying_wallet": _signer_address,
            "hint": "A 402 here usually means the wallet above needs more "
            "USDC/ETH on the configured network.",
        }
    if response.status_code != 200:
        try:
            detail = response.json()
        except ValueError:
            detail = response.text
        out = {"error": f"API returned HTTP {response.status_code}", "detail": detail}
        if 400 <= response.status_code < 500:
            out["charged"] = False
            out["note"] = (
                "The API refused the request before payment settled, so "
                "nothing was charged."
            )
        return out

    return response.json()


@mcp.tool()
async def crypto_sentiment(symbol: SymbolArg) -> dict[str, Any]:
    """
    Get real-time aggregate crypto sentiment for a ticker symbol (e.g. BTC, ETH, SOL).

    Gives the current reading; get_hourly_history gives past hourly readings.
    Unsupported coins and coins with too little recent news are refused and
    not charged.

    Pulls from 9 crypto news RSS outlets (CoinDesk, Cointelegraph, Decrypt,
    Bitcoin Magazine, The Block, CryptoSlate, NewsBTC, CryptoPotato, The
    Defiant) and the Fear & Greed Index, scored with VADER plus a crypto
    slang lexicon over headlines from the last 72 hours, weighted toward the
    newest.
    Returns a bullish/bearish/neutral label (or "insufficient recent news"),
    a compound score, a per-source breakdown and the headlines behind it.

    Each call pays this service's advertised x402 price in USDC on Base,
    charged to this MCP server's own configured wallet.
    """
    if (msg := _no_wallet("sentiment reading")) is not None:
        return msg

    symbol = symbol.upper().strip()
    return await _paid_get(f"{API_URL}/sentiment/{symbol}")


@mcp.tool()
async def get_hourly_history(
    symbol: str, start: TimeArg = None, end: TimeArg = None
) -> dict[str, Any]:
    """
    Get past hourly sentiment readings for one symbol (GET /history/{symbol}).

    Gives past readings, oldest first; crypto_sentiment gives the current
    one, and archive_coverage shows which coins have hourly history and
    since when. Readings are recorded live at the top of each UTC hour and
    never backfilled, so a missed hour stays missing.

    Defaults to the last 7 days; at most 30 days per call. The symbol has to
    be one archive_coverage lists as logged. Unlogged symbols, invalid
    ranges and ranges with no readings are refused and not charged; the
    refusal comes back as an error with the API's explanation.

    Each call pays $0.05 in USDC on Base, charged to this MCP server's own
    configured wallet.
    """
    if (msg := _no_wallet("hourly history")) is not None:
        return msg

    symbol = symbol.upper().strip()
    params = {k: v for k, v in (("start", start), ("end", end)) if v is not None}
    return await _paid_get(f"{API_URL}/history/{symbol}", params or None)


def _unsupported(symbol: str) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "supported": False,
        "detail": f"{symbol!r} is not a supported symbol. A crypto_sentiment "
        "call for it would be refused and not charged.",
        "supported_symbols": list(SUPPORTED_COINS),
    }


def _decode_challenge(response: httpx.Response) -> dict | None:
    """The 402's payment requirements: the base64 PAYMENT-REQUIRED header,
    else the JSON body (the API mirrors the header there)."""
    header = response.headers.get("payment-required")
    if header:
        try:
            return json.loads(base64.b64decode(header))
        except (ValueError, binascii.Error):
            pass
    try:
        return response.json()
    except ValueError:
        return None


@mcp.tool()
async def list_supported_coins() -> dict[str, Any]:
    """
    Free: list the 24 ticker symbols crypto_sentiment supports, with names.

    Useful before choosing a symbol for crypto_sentiment or price_quote.
    No wallet needed and no network call. Any other symbol is refused by the
    API before anything is charged.
    """
    return {
        "count": len(SUPPORTED_COINS),
        "coins": [{"symbol": s, "name": n} for s, n in SUPPORTED_COINS.items()],
    }


@mcp.tool()
async def archive_coverage() -> dict[str, Any]:
    """
    Free: what the hourly sentiment archive holds (GET /archive).

    Shows which coins have hourly history and since when, which is useful
    before a get_hourly_history call. Per symbol: first reading, row counts
    and missing hours. Readings are recorded live and never backfilled. No
    wallet needed; nothing is paid.
    """
    async with httpx.AsyncClient(timeout=60) as http:
        response = await http.get(
            f"{API_URL}/archive", headers={"Accept": "application/json"}
        )
    if response.status_code != 200:
        return {
            "error": f"API returned HTTP {response.status_code}",
            "detail": response.text,
        }
    return response.json()


@mcp.tool()
async def price_quote(symbol: SymbolArg) -> dict[str, Any]:
    """
    Free: what one crypto_sentiment call for a symbol would cost, and who it pays.

    Shows the cost and payTo address before a paid call. Makes one unpaid
    request and reads the x402 payment requirements from the 402 response:
    price in USD, network, asset and payTo address. Nothing is paid or
    signed, and no wallet is needed.
    """
    symbol = symbol.upper().strip()
    if symbol not in SUPPORTED_COINS:
        return _unsupported(symbol)

    async with httpx.AsyncClient(timeout=60) as http:
        response = await http.get(f"{API_URL}/sentiment/{symbol}")

    if response.status_code != 402:
        return {
            "error": f"expected HTTP 402 with payment requirements, got {response.status_code}",
            "detail": response.text,
            "note": "Nothing was paid.",
        }

    challenge = _decode_challenge(response) or {}
    accepts = challenge.get("accepts") or []
    if not accepts:
        return {
            "error": "402 response had no payment requirements",
            "detail": response.text,
            "note": "Nothing was paid.",
        }

    req = accepts[0]
    amount = req.get("amount") or req.get("maxAmountRequired")
    try:
        price_usd = int(amount) / 10**USDC_DECIMALS
    except (TypeError, ValueError):
        price_usd = None
    return {
        "symbol": symbol,
        "endpoint": f"{API_URL}/sentiment/{symbol}",
        "price_usd": price_usd,
        "amount_base_units": amount,
        "scheme": req.get("scheme"),
        "network": req.get("network"),
        "asset": req.get("asset"),
        "asset_name": (req.get("extra") or {}).get("name"),
        "payTo": req.get("payTo"),
        "note": "Quote only: nothing was paid or signed. Read from the live "
        "402 payment requirements.",
    }


def main() -> None:
    """Console-script entry point -- see pyproject.toml's [project.scripts]."""
    mcp.run()


if __name__ == "__main__":
    main()
