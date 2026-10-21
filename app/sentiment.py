"""
Sentiment scoring engine.

Uses VADER (rule-based, no API key, no training needed) as a base, then
layers a crypto-slang lexicon on top since VADER alone misses terms like
"moon", "rekt", "rug", "hodl", etc.
"""

import re

from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
from typing import List, Dict

_analyzer = SentimentIntensityAnalyzer()

# Crypto-specific slang not well covered by general-purpose sentiment
# lexicons. Values roughly follow VADER's -4..+4 intensity convention.
CRYPTO_LEXICON: Dict[str, float] = {
    "moon": 3.0, "mooning": 3.0, "bullish": 3.0, "bull run": 3.0,
    "ath": 2.0, "hodl": 1.5, "accumulate": 1.5, "undervalued": 2.0,
    "breakout": 2.0, "pump": 1.5,
    "rekt": -3.0, "rug": -3.5, "rug pull": -3.5, "bearish": -3.0,
    "dump": -2.5, "dumping": -2.5, "crash": -3.0, "capitulation": -3.0,
    "scam": -3.5, "ponzi": -3.5, "insolvent": -3.5, "hack": -3.0,
    "hacked": -3.0, "exploit": -2.5, "delisted": -2.5, "fud": -1.5,
    "overvalued": -2.0, "sell-off": -2.0, "liquidated": -2.5,
}

# VADER looks words up one at a time, so a multi-word entry ("bull run")
# never matched before 2026-10-21 (method 72h-hl24-v2). Each such phrase is
# now rewritten to one underscore-joined token ("bull_run", keeping the
# original case so VADER's caps emphasis still applies) that carries the
# entry's valence.
_PHRASES = [
    (re.compile(r"(?<!\w)" + r"\s+".join(map(re.escape, phrase.split())) + r"(?!\w)", re.IGNORECASE),
     phrase.replace(" ", "_"))
    for phrase in CRYPTO_LEXICON
    if " " in phrase
]

_analyzer.lexicon.update(CRYPTO_LEXICON)
_analyzer.lexicon.update({token: CRYPTO_LEXICON[token.replace("_", " ")] for _, token in _PHRASES})


def _join_phrases(text: str) -> str:
    for pattern, _ in _PHRASES:
        text = pattern.sub(lambda m: "_".join(m.group(0).split()), text)
    return text


def score_text(text: str) -> float:
    """VADER compound score (-1..1) for one snippet, as score_texts uses."""
    return _analyzer.polarity_scores(_join_phrases(text))["compound"]


def score_texts(texts: List[str]) -> Dict:
    """
    Score a list of text snippets and return an aggregate summary.
    """
    if not texts:
        return {
            "sample_size": 0,
            "average_compound": 0.0,
            "label": "no_data",
            "positive_pct": 0.0,
            "negative_pct": 0.0,
            "neutral_pct": 0.0,
        }

    compounds = []
    pos = neg = neu = 0

    for text in texts:
        scores = _analyzer.polarity_scores(_join_phrases(text))
        compound = scores["compound"]
        compounds.append(compound)
        if compound >= 0.05:
            pos += 1
        elif compound <= -0.05:
            neg += 1
        else:
            neu += 1

    n = len(texts)
    avg = sum(compounds) / n

    if avg >= 0.15:
        label = "bullish"
    elif avg <= -0.15:
        label = "bearish"
    else:
        label = "neutral"

    return {
        "sample_size": n,
        "average_compound": round(avg, 4),
        "label": label,
        "positive_pct": round(100 * pos / n, 1),
        "negative_pct": round(100 * neg / n, 1),
        "neutral_pct": round(100 * neu / n, 1),
    }
