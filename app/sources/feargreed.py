"""
Free, no-key market-wide sentiment signal: the Crypto Fear & Greed Index
(alternative.me). This is market-wide, not per-coin, but it's a useful
extra signal to blend into the final score.
"""

import httpx
from typing import Optional, Dict

FNG_URL = "https://api.alternative.me/fng/"


async def fetch_fear_greed() -> Optional[Dict]:
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            resp = await client.get(FNG_URL, params={"limit": 1})
            resp.raise_for_status()
            data = resp.json()
            entry = data.get("data", [{}])[0]
            return {
                "value": int(entry.get("value", 50)),
                "classification": entry.get("value_classification", "Neutral"),
            }
        except (httpx.HTTPError, ValueError, KeyError, IndexError):
            return None
