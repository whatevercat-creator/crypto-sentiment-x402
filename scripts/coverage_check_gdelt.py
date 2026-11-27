"""
Measurement only, not used by the service: how much English-language news
GDELT finds per coin over the last 72 hours, and how much of it comes from
the 9 outlets we read (app/sources/news.py's NEWS_OUTLETS).

Run on your Mac from the project folder:

    .venv/bin/python scripts/coverage_check_gdelt.py            # table only
    .venv/bin/python scripts/coverage_check_gdelt.py --titles   # also list titles
    .venv/bin/python scripts/coverage_check_gdelt.py --delay 20   # slower, if GDELT answers 429

For each of the 24 coins it queries the GDELT DOC 2.0 API
(https://api.gdeltproject.org/api/v2/doc/doc, mode=ArtList, format=json,
timespan=72h, maxrecords=250, English sources) with the coin's name and
prints: articles returned, distinct domains, and how many of those domains
are among our 9 outlets. 250 is GDELT's cap per request, so "250" means "at
least 250".

Polite by design: one request at a time, DELAY_SECONDS between them, a
User-Agent, and it stops at the first error (HTTP error, timeout, or a
non-JSON answer such as GDELT's "query too short" messages). Nothing is
written to disk; no article text is fetched (ArtList returns titles and
URLs only), and titles are printed only with --titles.
"""

import argparse
import sys
import time
from urllib.parse import urlsplit

import httpx

API = "https://api.gdeltproject.org/api/v2/doc/doc"
USER_AGENT = "crypto-sentiment-x402 coverage check (measurement script)"
# GDELT asks for at most one request every 5 seconds; 6 seconds still got a
# 429 on 2026-11-27, so the default leaves more room.
DELAY_SECONDS = 12
TIMEOUT_SECONDS = 30

# The domains of app/sources/news.py's NEWS_OUTLETS (kept here so this
# script runs without the app's settings).
OUR_DOMAINS = {
    "coindesk.com": "CoinDesk",
    "cointelegraph.com": "Cointelegraph",
    "decrypt.co": "Decrypt",
    "bitcoinmagazine.com": "Bitcoin Magazine",
    "theblock.co": "The Block",
    "cryptoslate.com": "CryptoSlate",
    "newsbtc.com": "NewsBTC",
    "cryptopotato.com": "CryptoPotato",
    "thedefiant.io": "The Defiant",
}

# The coin's name as GDELT searches it. Names that are ordinary words get a
# qualifier, so the count is about the coin (shown in the table's query
# column).
QUERIES = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "SOL": "solana",
    "XRP": "(ripple OR xrp)",
    "DOGE": "dogecoin",
    "ADA": "cardano",
    "AVAX": "avalanche crypto",
    "LINK": "chainlink",
    "MATIC": "(polygon OR matic) crypto",
    "DOT": "polkadot",
    "SHIB": '"shiba inu"',
    "LTC": "litecoin",
    "BNB": "bnb crypto",
    "USDC": '("usd coin" OR usdc)',
    "USDT": "tether crypto",
    "NEAR": '"near protocol"',
    "HYPE": "hyperliquid",
    "QNT": '"quant network"',
    "AAVE": "aave",
    "TAO": "bittensor",
    "PEPE": "pepe crypto",
    "PUMP": '"pump fun"',
    "ENA": "ethena",
    "STRK": "starknet",
}


def _domain(article: dict) -> str:
    domain = (article.get("domain") or urlsplit(article.get("url", "")).hostname or "").lower()
    return domain[4:] if domain.startswith("www.") else domain


def _ours(domain: str):
    for d, name in OUR_DOMAINS.items():
        if domain == d or domain.endswith("." + d):
            return name
    return None


def fetch(client: httpx.Client, query: str) -> list:
    resp = client.get(API, params={
        "query": f"{query} sourcelang:english",
        "mode": "ArtList",
        "format": "json",
        "timespan": "72h",
        "maxrecords": "250",
    })
    resp.raise_for_status()
    if not resp.content.strip():
        return []  # GDELT answers an empty body when nothing matched
    try:
        data = resp.json()
    except ValueError:
        raise RuntimeError(f"GDELT did not return JSON: {resp.text.strip()[:200]!r}")
    return data.get("articles") or []


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--titles", action="store_true", help="also print each article's title and domain")
    parser.add_argument("--delay", type=float, default=DELAY_SECONDS,
                        help=f"seconds between requests (default {DELAY_SECONDS}, minimum 5)")
    args = parser.parse_args()
    delay = max(args.delay, 5.0)

    print(f"{'coin':5} {'articles':>8} {'domains':>7} {'ours':>4}  outlets of ours seen / query")
    with httpx.Client(timeout=TIMEOUT_SECONDS, headers={"User-Agent": USER_AGENT}) as client:
        for i, (symbol, query) in enumerate(QUERIES.items()):
            if i:
                time.sleep(delay)
            try:
                articles = fetch(client, query)
            except (httpx.HTTPError, RuntimeError) as e:
                print(f"STOP at {symbol} ({query}): {type(e).__name__}: {e}", file=sys.stderr)
                return 1
            domains = {_domain(a) for a in articles if _domain(a)}
            ours = sorted({_ours(d) for d in domains} - {None})
            print(f"{symbol:5} {len(articles):8} {len(domains):7} {len(ours):4}  "
                  f"{', '.join(ours) or '-'} / {query}", flush=True)
            if args.titles:
                for a in articles:
                    print(f"        {_domain(a)}: {a.get('title', '').strip()}")
    print("\narticles = GDELT ArtList results over 72h (250 = the cap, so at least 250); "
          "domains = distinct source domains; ours = how many of those are among our 9 outlets.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
