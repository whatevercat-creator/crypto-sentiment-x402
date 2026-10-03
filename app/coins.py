"""
Minimal symbol -> full name map for the coins this API supports out of
the box, plus the terms the news matcher looks for. Add more as needed.

Matching (see match_pattern) is whole-word and case-insensitive: the
ticker, the name and any COIN_ALIASES. Until 2026-10-03 it was a plain
substring test, so "ETH" matched "whether" and "SOL" matched "solution"
(see MATCHER_CHANGED_ON and app/validation.json's methodology_changes).
"""

import re
from functools import lru_cache

# Date the whole-word matcher replaced substring matching.
MATCHER_CHANGED_ON = "2026-10-03"
# Stored with each hourly reading made by the whole-word matcher.
MATCHER_VERSION = "whole_word"

COIN_NAMES = {
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

# The coins tracked before the 2026-10-03 names were added; the daily
# dataset (app/dataset.py) keeps snapshotting exactly these by default.
ORIGINAL_SYMBOLS = (
    "BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "AVAX", "LINK", "MATIC", "DOT",
    "SHIB", "LTC", "BNB", "USDC", "USDT",
)

# Extra whole-word terms for a coin, beyond its ticker and name.
COIN_ALIASES = {
    "BTC": ("bitcoins",),
    "ETH": ("Ether",),
    "NEAR": ("NEAR Intents",),
    "PUMP": ("PumpFun", "pump fun"),
    "QNT": ("Quant Network",),
}

# Tickers that are ordinary words: the bare word doesn't count, only the
# name, an alias or the "$TICKER" form ("$LINK", not "link").
WORD_TICKERS = frozenset({"LINK", "DOT", "NEAR", "PUMP", "HYPE", "TAO"})

# Names that are ordinary words ("quant funds"): matched only through the
# ticker or an alias.
WORD_NAMES = frozenset({"QNT"})


def resolve_name(symbol: str) -> str:
    return COIN_NAMES.get(symbol.upper(), symbol)


def validate_symbol(symbol: str) -> str:
    """Normalize and validate a ticker symbol (e.g. 'btc ' -> 'BTC'). Raises ValueError if invalid."""
    symbol = symbol.upper().strip()
    if not symbol.isalnum() or len(symbol) > 10:
        raise ValueError(f"Invalid symbol: {symbol!r}")
    return symbol


def _term_regex(term: str) -> str:
    return r"\s+".join(re.escape(part) for part in term.split())


@lru_cache(maxsize=256)
def match_pattern(symbol: str) -> re.Pattern:
    """Whole-word, case-insensitive pattern for headlines about `symbol`."""
    symbol = symbol.upper()
    terms = list(COIN_ALIASES.get(symbol, ()))
    name = COIN_NAMES.get(symbol)
    if name and symbol not in WORD_NAMES:
        terms.append(name)
    alternatives = [_term_regex(t) for t in terms]
    if symbol in WORD_TICKERS:
        alternatives.append(r"\$" + re.escape(symbol))
    else:
        alternatives.append(r"\$?" + re.escape(symbol))
    body = "|".join(f"(?:{a})" for a in alternatives)
    return re.compile(rf"(?<![\w$])(?:{body})(?!\w)", re.IGNORECASE)


_TAG = re.compile(r"<[^>]+>")


def mentions(symbol: str, text: str) -> bool:
    """True if `text` (a headline plus description, which may contain HTML)
    mentions `symbol`. Tags are stripped first so link URLs such as
    bitcoinmagazine.com don't count as a mention."""
    return bool(match_pattern(symbol).search(_TAG.sub(" ", text)))
