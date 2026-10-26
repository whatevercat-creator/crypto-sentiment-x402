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
  SENTIMENT_PAY_TO       - payTo address the paid tools expect in the 402
                           (default: the hosted API's wallet). Set it when
                           SENTIMENT_API_URL points at your own deployment.

Before signing, each paid call checks the 402 it got: the amount must not
exceed that tool's price (TOOL_PRICES_USD) and payTo, network and asset must
match. Anything else is refused and nothing is paid.

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
from contextvars import ContextVar

import httpx
from cdp import CdpClient
from cdp.evm_local_account import EvmLocalAccount
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field
from x402 import AbortResult, NoMatchingRequirementsError, x402Client
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
# get_hourly_history takes any string (the API validates it); description only.
HistorySymbolArg = Annotated[
    str,
    Field(
        description="Crypto ticker, e.g. BTC; uppercased and trimmed before "
        "the request. archive_coverage lists the logged symbols."
    ),
]

FREE_TOOLS = ("list_supported_coins", "archive_coverage", "price_quote")

# Tool annotations. Free tools only read; each paid call charges the wallet,
# so it isn't read-only or idempotent. All but list_supported_coins (a
# local list, no network call) reach the live API.
LOCAL_TOOL_ANNOTATIONS = ToolAnnotations(
    read_only_hint=True,
    idempotent_hint=True,
    destructive_hint=False,
    open_world_hint=False,
)
FREE_TOOL_ANNOTATIONS = ToolAnnotations(
    read_only_hint=True,
    idempotent_hint=True,
    destructive_hint=False,
    open_world_hint=True,
)
PAID_TOOL_ANNOTATIONS = ToolAnnotations(
    read_only_hint=False,
    idempotent_hint=False,
    destructive_hint=False,
    open_world_hint=True,
)

# USDC uses 6 decimals on Base and Base Sepolia; 402 amounts are base units.
USDC_DECIMALS = 6

# What each paid tool expects the 402 to ask for (app/billing.py's
# X402_PRICE_USD and app/archive.py's X402_HISTORY_PRICE_USD defaults).
# _check_requirements refuses to sign for more, or for anyone else.
TOOL_PRICES_USD = {"crypto_sentiment": 0.01, "get_hourly_history": 0.05}
USDC_ASSET = {
    "eip155:8453": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    "eip155:84532": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
}[CAIP2_NETWORK]
PAY_TO_ADDRESS = os.environ.get(
    "SENTIMENT_PAY_TO", "0xA6E10884a70D00DE8b8c5dfF0a2a9B1a821cE074"
)

# The current paid call's expected price, set by _paid_get and read by the
# payment client's before-signing hook. A refusal is recorded back into it.
_expected_payment: ContextVar[dict[str, Any] | None] = ContextVar(
    "_expected_payment", default=None
)

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
    _payment_client.on_before_payment_creation(_check_requirements)
    return _payment_client


def _check_requirements(context: Any) -> AbortResult | None:
    """Before-signing hook: abort unless the requirements the SDK is about to
    sign match the current tool's price, payTo, network and asset. Fails
    closed when no paid tool set an expectation."""
    expected = _expected_payment.get()
    if expected is None:
        return AbortResult(reason="no expected price set for this payment")

    req = context.selected_requirements
    amount = getattr(req, "amount", None) or getattr(req, "max_amount_required", None)
    problems = []
    try:
        over = int(amount) > expected["amount"]
    except (TypeError, ValueError):
        over = True
    if over:
        problems.append(
            f"amount {amount!r} is above the expected {expected['amount']} "
            f"(${expected['price_usd']})"
        )
    if str(req.pay_to).lower() != PAY_TO_ADDRESS.lower():
        problems.append(f"payTo {req.pay_to!r} is not the expected {PAY_TO_ADDRESS}")
    if req.network != CAIP2_NETWORK:
        problems.append(f"network {req.network!r} is not the expected {CAIP2_NETWORK}")
    if str(req.asset).lower() != USDC_ASSET.lower():
        problems.append(f"asset {req.asset!r} is not the expected USDC {USDC_ASSET}")

    if problems:
        expected["refused"] = problems
        return AbortResult(reason="; ".join(problems))
    return None


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


async def _paid_get(
    tool: str, url: str, params: dict[str, str] | None = None
) -> dict[str, Any]:
    """GET a paid endpoint through the x402 payment client, paying at most
    TOOL_PRICES_USD[tool] to PAY_TO_ADDRESS."""
    payment_client = await _get_payment_client()

    price_usd = TOOL_PRICES_USD[tool]
    expected = {
        "price_usd": price_usd,
        "amount": round(price_usd * 10**USDC_DECIMALS),
        "refused": None,
    }
    token = _expected_payment.set(expected)
    try:
        async with x402HttpxClient(payment_client) as http:
            response = await http.get(url, params=params)
            await response.aread()
    except Exception as exc:
        # The SDK wraps the hook's abort in its own PaymentError. Its own
        # pre-signing filters (spend_controls' default assets, the registered
        # network) can also reject the 402 before the hook runs; both mean
        # nothing was signed. Anything else is re-raised.
        if expected["refused"] is None:
            cause = exc
            while cause is not None and not isinstance(cause, NoMatchingRequirementsError):
                cause = cause.__cause__
            if cause is None:
                raise
            expected["refused"] = [f"no payment requirement this client will sign: {cause}"]
    finally:
        _expected_payment.reset(token)

    if expected["refused"] is not None:
        return {
            "error": "refused: price/payTo mismatch, nothing was paid",
            "detail": expected["refused"],
            "expected": {
                "price_usd": price_usd,
                "amount_base_units": str(expected["amount"]),
                "payTo": PAY_TO_ADDRESS,
                "network": CAIP2_NETWORK,
                "asset": USDC_ASSET,
            },
            "charged": False,
            "note": "The 402's payment requirements didn't match what this "
            "tool expects, so nothing was signed or sent.",
        }

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


@mcp.tool(title="Current Crypto Sentiment", annotations=PAID_TOOL_ANNOTATIONS)
async def crypto_sentiment(symbol: SymbolArg) -> dict[str, Any]:
    """
    Current news sentiment for one crypto ticker. Paid: $0.01 in USDC on Base
    per call, from this MCP server's own CDP wallet.

    symbol must be exactly one of the 24 uppercase tickers from
    list_supported_coins (e.g. BTC). Lowercase ("btc") or any other symbol is
    rejected by the input schema before a request is sent, so nothing is
    charged.

    Scores headlines from the last 72 hours across 9 crypto news RSS outlets
    (VADER plus a crypto slang lexicon, weighted toward the newest) alongside
    the Fear & Greed Index. Returns a bullish/bearish/neutral label, a
    compound score (-1 to 1), sample sizes, the news and Fear & Greed values,
    and up to 5 headlines that moved the score.

    Low-coverage coins: when recent headlines are too few (effective sample
    size below 5), the API refuses with HTTP 422 "insufficient recent news",
    returned here as an error with charged: false. archive_coverage shows each
    coin's coverage_7d_pct. Without CDP keys the tool returns a no-wallet
    message and pays nothing. For past readings use get_hourly_history.
    """
    if (msg := _no_wallet("sentiment reading")) is not None:
        return msg

    symbol = symbol.upper().strip()
    return await _paid_get("crypto_sentiment", f"{API_URL}/sentiment/{symbol}")


@mcp.tool(title="Hourly Sentiment History", annotations=PAID_TOOL_ANNOTATIONS)
async def get_hourly_history(
    symbol: HistorySymbolArg, start: TimeArg = None, end: TimeArg = None
) -> dict[str, Any]:
    """
    Past hourly sentiment readings for one coin, oldest first. Paid: $0.05 in
    USDC on Base per call, from this MCP server's own CDP wallet.

    Range: omit both bounds for the 7 days up to now. With only end, the range
    starts 7 days before it; with only start, it runs to now. A reading is
    included when start <= observed_at < end. The span can be at most 30 days,
    so split longer periods into several calls. Readings are recorded live at
    the top of each UTC hour and never backfilled; a missed hour stays missing.

    Refused and not charged (returned as an error with charged: false):
    - 400: malformed symbol or time, start not before end, or a span over 30 days.
    - 404: symbol not in the archive.
    - 404 no_readings_in_range: no readings in the range (gives first_reading).
    - 404 no_headlines_in_range: every hour in the range had zero matching
      headlines (gives rows_in_range and coverage_7d_pct).
    A range with at least one hour of headlines returns all its rows.

    For low-coverage coins, check archive_coverage first: its coverage_7d_pct
    and first_reading show where data exists. Without CDP keys the tool returns
    a no-wallet message and pays nothing. For the current reading use
    crypto_sentiment.
    """
    if (msg := _no_wallet("hourly history")) is not None:
        return msg

    symbol = symbol.upper().strip()
    params = {k: v for k, v in (("start", start), ("end", end)) if v is not None}
    return await _paid_get(
        "get_hourly_history", f"{API_URL}/history/{symbol}", params or None
    )


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


@mcp.tool(title="List Supported Coins", annotations=LOCAL_TOOL_ANNOTATIONS)
async def list_supported_coins() -> dict[str, Any]:
    """
    Free: list the 24 ticker symbols crypto_sentiment supports, with names.

    Useful before choosing a symbol for crypto_sentiment or price_quote.
    No wallet needed and no network call. Any other symbol is rejected by
    those tools' input schema before a request is sent, so nothing is
    charged.
    """
    return {
        "count": len(SUPPORTED_COINS),
        "coins": [{"symbol": s, "name": n} for s, n in SUPPORTED_COINS.items()],
    }


@mcp.tool(title="Archive Coverage", annotations=FREE_TOOL_ANNOTATIONS)
async def archive_coverage() -> dict[str, Any]:
    """
    Free: what the hourly sentiment archive holds (GET /archive). No wallet
    needed.

    Per symbol: first and latest reading, row counts, missing hours, and
    coverage_7d_pct, the share of the last 7 days' logged hours that had at
    least one matching headline (with hours_with_headlines_7d and
    hours_logged_7d). Check it before get_hourly_history to pick a symbol and
    range with data: low-coverage coins often get no_headlines_in_range from
    get_hourly_history or "insufficient recent news" from crypto_sentiment,
    both refused and not charged.
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


@mcp.tool(title="Price Quote", annotations=FREE_TOOL_ANNOTATIONS)
async def price_quote(symbol: SymbolArg) -> dict[str, Any]:
    """
    Free: the live x402 price of one crypto_sentiment call, read from an
    unpaid request's 402 response. Nothing is signed; no wallet needed.

    When not to use it: prices are fixed at $0.01 per crypto_sentiment call
    and $0.05 per get_hourly_history call, so there's no need to call this
    before every paid call. Use it to confirm the live price and payment
    requirements, e.g. before funding a wallet. It quotes crypto_sentiment
    only.

    symbol must be exactly one of the 24 uppercase tickers from
    list_supported_coins; lowercase or any other symbol is rejected by the
    input schema before a request is sent. A low-coverage coin still gets a
    quote; a crypto_sentiment call for it may then be refused with
    "insufficient recent news" (not charged).
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
