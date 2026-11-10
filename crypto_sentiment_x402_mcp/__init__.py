"""
MCP server for the Crypto Sentiment API (x402).

Lets any MCP client (Claude Desktop, Claude Code, etc.) call the pay-per-use
sentiment API as tools. The paid tools (get_sentiment, get_sentiment_history
and get_movers) pay for themselves via x402, using a Coinbase Developer
Platform (CDP) wallet that belongs to whoever RUNS this MCP server -- there's
no free tier and no subsidized calls. If you're handing this server to
someone else to run, they fund the wallet, not you.

Four tools are free and need no wallet: list_supported_coins,
get_archive_coverage, get_call_price and get_market_sentiment. The server
starts without any CDP variables; only the paid tools need them.

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
  MAX_SPEND_USD_PER_SESSION - most this server process will sign for in
                           total, in USD (default 0.50; 0 turns the cap off).

Before signing, each paid call checks the 402 it got: the amount must not
exceed that tool's price (TOOL_PRICES_USD) and payTo, network and asset must
match. It also refuses when the payment would take this process's total
over MAX_SPEND_USD_PER_SESSION. Anything refused is never signed, so
nothing is paid.

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
# get_sentiment_history takes any string (the API validates it); description only.
HistorySymbolArg = Annotated[
    str,
    Field(
        description="Crypto ticker, e.g. BTC; uppercased and trimmed before "
        "the request. get_archive_coverage lists the logged symbols."
    ),
]

MoversLimitArg = Annotated[
    int | None,
    Field(
        ge=1,
        le=24,
        description="Max coins in movers_up and in movers_down, 1 to 24, "
        "e.g. 5. Omit for the API's default of 10.",
    ),
]

FREE_TOOLS = (
    "list_supported_coins",
    "get_archive_coverage",
    "get_call_price",
    "get_market_sentiment",
)

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
# app/market.py's X402_MOVERS_PRICE_USD default ($0.02) for get_movers.
TOOL_PRICES_USD = {
    "get_sentiment": 0.01,
    "get_sentiment_history": 0.05,
    "get_movers": 0.02,
}
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

# Session spend cap: the most this server process signs for in total.
# Counted when a payment is approved for signing (the before-signing hook),
# so a call the API later refuses still counts -- the conservative side.
DEFAULT_MAX_SPEND_USD = "0.50"
_spent_base_units = 0


def _spend_cap_base_units() -> int:
    """MAX_SPEND_USD_PER_SESSION in USDC base units; 0 means no cap. A value
    that isn't a non-negative number falls back to the default, never to
    no cap."""
    raw = os.environ.get("MAX_SPEND_USD_PER_SESSION", DEFAULT_MAX_SPEND_USD)
    try:
        usd = float(raw.strip().lstrip("$"))
    except ValueError:
        usd = float(DEFAULT_MAX_SPEND_USD)
    if usd != usd or usd < 0 or usd == float("inf"):  # NaN, negative, inf
        usd = float(DEFAULT_MAX_SPEND_USD)
    return round(usd * 10**USDC_DECIMALS)


def _over_cap(amount: int) -> bool:
    cap = _spend_cap_base_units()
    return cap > 0 and _spent_base_units + amount > cap


def _usd(base_units: int) -> str:
    """$0.01, $0.50, $0.015: at least 2 decimals, never rounded."""
    whole, frac = divmod(base_units, 10**USDC_DECIMALS)
    frac_s = f"{frac:0{USDC_DECIMALS}d}".rstrip("0").ljust(2, "0")
    return f"${whole}.{frac_s}"


def _cap_refusal(amount: int) -> dict[str, Any]:
    cap = _spend_cap_base_units()
    return {
        "error": "refused: session spend cap reached, nothing was paid",
        "detail": f"This call ({_usd(amount)}) would take this server's "
        f"spending to {_usd(_spent_base_units + amount)}, over "
        f"MAX_SPEND_USD_PER_SESSION ({_usd(cap)}). Already spent this "
        f"session: {_usd(_spent_base_units)}.",
        "charged": False,
        "spent_usd": _spent_base_units / 10**USDC_DECIMALS,
        "cap_usd": cap / 10**USDC_DECIMALS,
        "how_to_raise": "Set MAX_SPEND_USD_PER_SESSION in this MCP server's "
        "env to a higher USD amount (e.g. 2.00), or 0 to turn the cap off, "
        "then restart the server. The count starts again at $0.00 on restart.",
        "free_tools": list(FREE_TOOLS),
    }


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
    sign match the current tool's price, payTo, network and asset, and
    stay within the session spend cap. Fails closed when no paid tool set
    an expectation. An approved payment is counted toward the cap here."""
    global _spent_base_units
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
    if _over_cap(int(amount)):
        expected["cap_refused"] = int(amount)
        return AbortResult(reason="session spend cap (MAX_SPEND_USD_PER_SESSION) reached")
    _spent_base_units += int(amount)
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
        "charged": False,
        "free_tools": list(FREE_TOOLS),
        "hint": "list_supported_coins, get_archive_coverage, "
        "get_call_price and get_market_sentiment are free and need no wallet.",
    }


async def _paid_get(
    tool: str, url: str, params: dict[str, str] | None = None
) -> dict[str, Any]:
    """GET a paid endpoint through the x402 payment client, paying at most
    TOOL_PRICES_USD[tool] to PAY_TO_ADDRESS, within the session spend cap."""
    price_usd = TOOL_PRICES_USD[tool]
    amount = round(price_usd * 10**USDC_DECIMALS)
    # Refuse up front when even the listed price would break the cap; the
    # before-signing hook checks again with the amount actually asked for.
    if _over_cap(amount):
        return _cap_refusal(amount)

    payment_client = await _get_payment_client()

    expected = {
        "price_usd": price_usd,
        "amount": amount,
        "refused": None,
        "cap_refused": None,
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
        if expected["refused"] is None and expected["cap_refused"] is None:
            cause = exc
            while cause is not None and not isinstance(cause, NoMatchingRequirementsError):
                cause = cause.__cause__
            if cause is None:
                raise
            expected["refused"] = [f"no payment requirement this client will sign: {cause}"]
    finally:
        _expected_payment.reset(token)

    if expected["cap_refused"] is not None:
        return _cap_refusal(expected["cap_refused"])
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


@mcp.tool(title="Current Crypto News Sentiment (paid)", annotations=PAID_TOOL_ANNOTATIONS)
async def get_sentiment(symbol: SymbolArg) -> dict[str, Any]:
    """
    Crypto news sentiment for one coin right now: a bullish/bearish/neutral
    label, a score from -1 to 1, sample sizes, the Fear & Greed value and up
    to 5 headlines that moved the score. Paid: $0.01 in USDC on Base per
    call, from this MCP server's own CDP wallet.

    Use when an agent needs the current news mood for a crypto ticker.
    Scores headlines from the last 72 hours across 9 crypto news RSS outlets,
    newest weighted most. Each call fetches and scores the feeds live (no
    cache), so repeat calls can differ as news arrives. For past readings
    use get_sentiment_history; for the coins shifting most use get_movers.

    symbol picks the coin and goes into the request path: one of the 24
    uppercase tickers from list_supported_coins. Any other symbol is
    rejected by the input schema, so nothing is charged. Too few recent
    headlines: HTTP 422 "insufficient recent news", returned with charged:
    false (get_archive_coverage shows coverage_7d_pct). Without CDP keys or
    past the session spend cap, it pays nothing and says why.
    """
    if (msg := _no_wallet("sentiment reading")) is not None:
        return msg

    symbol = symbol.upper().strip()
    return await _paid_get("get_sentiment", f"{API_URL}/sentiment/{symbol}")


@mcp.tool(title="Hourly Crypto Sentiment History (paid)", annotations=PAID_TOOL_ANNOTATIONS)
async def get_sentiment_history(
    symbol: HistorySymbolArg, start: TimeArg = None, end: TimeArg = None
) -> dict[str, Any]:
    """
    Historical crypto news sentiment for one coin: hourly readings (score,
    sample size, Fear & Greed and the top headlines for each hour), oldest
    first. Paid: $0.05 in USDC on Base per call, from this MCP server's own
    CDP wallet.

    Use when an agent needs how a coin's news sentiment moved over past hours
    or days, e.g. to line it up with price. For the current reading use
    get_sentiment.

    symbol goes into the request path (/history/{symbol}) and start/end are
    sent as query parameters, so together they pick one coin and one time
    range. Omit both for the 7 days up to now; with only end the range starts
    7 days before it, with only start it runs to now. A reading is included
    when start <= observed_at < end; at most 30 days per call. Readings are
    recorded live at the top of each UTC hour and never backfilled.

    Refused and not charged (an error with charged: false): 400 for a
    malformed symbol or time, start not before end, or over 30 days; 404 for
    a symbol not in the archive, no_readings_in_range (gives first_reading)
    or no_headlines_in_range (no hour had headlines). For low-coverage coins,
    check get_archive_coverage first. Without CDP keys or past the session
    spend cap, it pays nothing and says why.
    """
    if (msg := _no_wallet("hourly history")) is not None:
        return msg

    symbol = symbol.upper().strip()
    params = {k: v for k, v in (("start", start), ("end", end)) if v is not None}
    return await _paid_get(
        "get_sentiment_history", f"{API_URL}/history/{symbol}", params or None
    )


@mcp.tool(title="Biggest 24h Crypto Sentiment Movers (paid)", annotations=PAID_TOOL_ANNOTATIONS)
async def get_movers(limit: MoversLimitArg = None) -> dict[str, Any]:
    """
    Crypto sentiment movers: the coins whose news sentiment rose or fell the
    most over the last ~24 hours, ranked across all 24 supported coins in one
    call. Paid: $0.02 in USDC on Base per call, from this MCP server's own
    CDP wallet.

    Use when an agent wants to know which coins' news mood is shifting
    without calling get_sentiment for every coin. Compares each coin's
    current reading (refreshed at most hourly; computed_at says when) with
    its archived reading closest to 24 hours earlier. Returns movers_up
    (rises, largest first) and movers_down (falls, largest first) with each
    coin's current score, previous score and delta.

    limit is sent as the ?limit= query parameter and caps movers_up and
    movers_down each; omit it for 10. Coins without enough recent news, now
    or ~24 hours ago, are left out. A call that can rank no coin is refused
    with HTTP 422, returned as an error with charged: false. Without CDP
    keys or past the session spend cap, it pays nothing and says why.
    """
    if (msg := _no_wallet("movers ranking")) is not None:
        return msg

    params = {"limit": str(limit)} if limit is not None else None
    return await _paid_get("get_movers", f"{API_URL}/movers", params)


def _unsupported(symbol: str) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "supported": False,
        "detail": f"{symbol!r} is not a supported symbol. A get_sentiment "
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


async def _free_get(path: str) -> dict[str, Any]:
    """GET a free API endpoint as JSON; no wallet, nothing signed."""
    async with httpx.AsyncClient(timeout=60) as http:
        response = await http.get(
            f"{API_URL}{path}", headers={"Accept": "application/json"}
        )
    if response.status_code != 200:
        return {
            "error": f"API returned HTTP {response.status_code}",
            "detail": response.text,
        }
    return response.json()


@mcp.tool(title="List Supported Crypto Coins (free)", annotations=LOCAL_TOOL_ANNOTATIONS)
async def list_supported_coins() -> dict[str, Any]:
    """
    The 24 crypto tickers (symbol and name) that get_sentiment and
    get_call_price accept. Free: no wallet needed and no network call.

    Use when choosing a symbol before calling get_sentiment or
    get_call_price. The coin list is fixed per release: new coins come with a
    new version of this package, never while the server runs. Any other
    symbol is rejected by those tools' input schema before a request is
    sent, so nothing is charged.
    """
    return {
        "count": len(SUPPORTED_COINS),
        "coins": [{"symbol": s, "name": n} for s, n in SUPPORTED_COINS.items()],
    }


@mcp.tool(title="Sentiment Archive Coverage (free)", annotations=FREE_TOOL_ANNOTATIONS)
async def get_archive_coverage() -> dict[str, Any]:
    """
    Free: what the hourly sentiment archive holds (GET /archive). No wallet
    needed.

    Per symbol: first and latest reading, row counts, missing hours, and
    coverage_7d_pct, the share of the last 7 days' logged hours that had at
    least one matching headline (with hours_with_headlines_7d and
    hours_logged_7d). Check it before get_sentiment_history to pick a symbol
    and range with data: low-coverage coins often get no_headlines_in_range
    from get_sentiment_history or "insufficient recent news" from
    get_sentiment, both refused and not charged.
    """
    return await _free_get("/archive")


@mcp.tool(title="Crypto Market Sentiment Index (free)", annotations=FREE_TOOL_ANNOTATIONS)
async def get_market_sentiment() -> dict[str, Any]:
    """
    Crypto market sentiment index: one news-sentiment score and
    bullish/bearish/neutral label for the whole crypto market, aggregated
    from the 24 supported coins and weighted by how much recent news each
    has, with the contributing coins listed. Free: no wallet needed.

    Use when an agent needs the overall crypto news mood rather than one
    coin's; use get_sentiment for a single coin and get_movers for which
    coins are shifting. Refreshed at most hourly: cached, computed_at and
    next_refresh_at in the response say how fresh it is, and a call before
    next_refresh_at returns the same index.
    """
    return await _free_get("/market-sentiment")


@mcp.tool(title="Live x402 Price of a Sentiment Call (free)", annotations=FREE_TOOL_ANNOTATIONS)
async def get_call_price(symbol: SymbolArg) -> dict[str, Any]:
    """
    Free: the live x402 price of one get_sentiment call, read from an
    unpaid request's 402 response. Nothing is signed; no wallet needed.

    When not to use it: prices are fixed at $0.01 per get_sentiment call,
    $0.05 per get_sentiment_history call and $0.02 per get_movers call, so
    there's no need to call this before every paid call. Use it to confirm
    the live price and payment requirements, e.g. before funding a wallet.
    It quotes get_sentiment only.

    symbol must be exactly one of the 24 uppercase tickers from
    list_supported_coins; lowercase or any other symbol is rejected by the
    input schema before a request is sent. A low-coverage coin still gets a
    quote; a get_sentiment call for it may then be refused with
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
