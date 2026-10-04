"""
Time window and recency weighting for the sentiment score (2026-10-04).

Before this, the score was a plain average over whatever the feeds held,
however old (up to 30 items per feed; one outlet was serving five-month-old
items). Now:

- Only headlines published in the last MAX_AGE_HOURS count. Headlines with
  no parseable publish date are left out: their age can't be checked, and a
  stale feed is exactly where they'd come from. (None of the 224 items
  fetched on 2026-10-04 lacked a date.)
- Each headline's weight halves for every HALF_LIFE_HOURS of age:
  weight = 0.5 ** (age_hours / HALF_LIFE_HOURS).
- The score (average_compound) and label use the weighted average. The
  unweighted 72-hour average is returned alongside it.
- effective_sample_size = (sum of weights)^2 / sum of squared weights: how
  many full-weight headlines the weighted sample is worth. Below
  MIN_EFFECTIVE_SAMPLE the label is INSUFFICIENT_LABEL instead of
  bullish/bearish/neutral.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional

from app.sentiment import score_text

WINDOW_CHANGED_ON = "2026-10-04"
# Recorded with each hourly row and returned in every response; bump it
# whenever the window or weighting changes.
WINDOW_VERSION = "72h-hl24"
MAX_AGE_HOURS = 72
HALF_LIFE_HOURS = 24
MIN_EFFECTIVE_SAMPLE = 5
INSUFFICIENT_LABEL = "insufficient recent news"
# Feed clocks can run slightly ahead; a headline dated up to this far in the
# future counts as age 0, anything further ahead is a bad date and is dropped.
FUTURE_TOLERANCE_HOURS = 1
# Characters of title + description that get scored per headline.
SCORED_CHARS = 500
# Same cut-offs as before for the label and the positive/negative counts.
BULLISH_AT, BEARISH_AT = 0.15, -0.15
POSITIVE_AT, NEGATIVE_AT = 0.05, -0.05


def parse_published(published: Optional[str]) -> Optional[datetime]:
    if not published:
        return None
    try:
        dt = datetime.fromisoformat(published.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def age_hours(published: Optional[str], now: datetime) -> Optional[float]:
    """Age in hours, or None if the date is missing, unparseable or more than
    FUTURE_TOLERANCE_HOURS in the future."""
    dt = parse_published(published)
    if dt is None:
        return None
    age = (now - dt).total_seconds() / 3600
    if age < -FUTURE_TOLERANCE_HOURS:
        return None
    return max(age, 0.0)


def weight_for(age: float) -> float:
    return 0.5 ** (age / HALF_LIFE_HOURS)


@dataclass(frozen=True)
class Scored:
    """A headline inside the window, with its own score and weight."""

    headline: object  # app.sources.news.Headline
    score: float
    age_hours: float
    weight: float


def in_window(headlines: list, now: Optional[datetime] = None) -> List[Scored]:
    """The headlines published within MAX_AGE_HOURS, scored and weighted,
    in their original order."""
    now = now or datetime.now(timezone.utc)
    out = []
    for h in headlines:
        age = age_hours(h.published, now)
        if age is None or age > MAX_AGE_HOURS:
            continue
        out.append(Scored(h, score_text(h.text[:SCORED_CHARS]), age, weight_for(age)))
    return out


def summarize(scored: List[Scored]) -> dict:
    """The overall_sentiment block for headlines already windowed."""
    n = len(scored)
    if n == 0:
        return {
            "sample_size": 0,
            "average_compound": 0.0,
            "label": INSUFFICIENT_LABEL,
            "positive_pct": 0.0,
            "negative_pct": 0.0,
            "neutral_pct": 0.0,
            "unweighted_compound_72h": 0.0,
            "effective_sample_size": 0.0,
            "newest_headline_age_hours": None,
            "window": WINDOW_VERSION,
        }

    total_weight = sum(s.weight for s in scored)
    weighted = sum(s.weight * s.score for s in scored) / total_weight
    unweighted = sum(s.score for s in scored) / n
    effective = total_weight ** 2 / sum(s.weight ** 2 for s in scored)

    if effective < MIN_EFFECTIVE_SAMPLE:
        label = INSUFFICIENT_LABEL
    elif weighted >= BULLISH_AT:
        label = "bullish"
    elif weighted <= BEARISH_AT:
        label = "bearish"
    else:
        label = "neutral"

    pos = sum(s.score >= POSITIVE_AT for s in scored)
    neg = sum(s.score <= NEGATIVE_AT for s in scored)
    return {
        "sample_size": n,
        "average_compound": round(weighted, 4),
        "label": label,
        "positive_pct": round(100 * pos / n, 1),
        "negative_pct": round(100 * neg / n, 1),
        "neutral_pct": round(100 * (n - pos - neg) / n, 1),
        "unweighted_compound_72h": round(unweighted, 4),
        "effective_sample_size": round(effective, 2),
        "newest_headline_age_hours": round(min(s.age_hours for s in scored), 2),
        "window": WINDOW_VERSION,
    }
