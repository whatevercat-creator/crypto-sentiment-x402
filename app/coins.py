"""
Minimal symbol -> full name map for the coins this API supports out of
the box. Add more as needed.
"""

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
}


def resolve_name(symbol: str) -> str:
    return COIN_NAMES.get(symbol.upper(), symbol)


def validate_symbol(symbol: str) -> str:
    """Normalize and validate a ticker symbol (e.g. 'btc ' -> 'BTC'). Raises ValueError if invalid."""
    symbol = symbol.upper().strip()
    if not symbol.isalnum() or len(symbol) > 10:
        raise ValueError(f"Invalid symbol: {symbol!r}")
    return symbol
