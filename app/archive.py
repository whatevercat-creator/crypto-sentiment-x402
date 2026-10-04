"""
The hourly sentiment archive as a product: what exists (GET /archive, free)
and the readings themselves (GET /history/{symbol}, paid per call over x402
at X402_HISTORY_PRICE_USD; wired up in app/main.py).

Everything here reads the sentiment_hourly table app/hourly.py writes.
Readings are recorded live at the top of each UTC hour and never
backfilled: an hour the logger missed stays missing, and /archive lists it.

Env vars (see .env.example):
  X402_HISTORY_PRICE_USD - price per /history call (default "$0.05")
"""

import json
import os
from datetime import datetime, timedelta, timezone
from html import escape
from typing import Optional

from app.billing import _db
from app.coins import MATCHER_CHANGED_ON, MATCHER_VERSION
from app.hourly import HOURLY_SYMBOLS
from app.window import HALF_LIFE_HOURS, MAX_AGE_HOURS, WINDOW_CHANGED_ON, WINDOW_VERSION

HISTORY_PRICE_USD = os.environ.get("X402_HISTORY_PRICE_USD", "$0.05")
DEFAULT_RANGE = timedelta(days=7)
MAX_RANGE = timedelta(days=30)
HOUR = timedelta(hours=1)
# The logger writes a few seconds after :00; until this far into an hour, its
# reading isn't counted as missing yet.
_GRACE = timedelta(minutes=10)

RECORDING_NOTE = (
    "Readings are recorded live at the top of every UTC hour and never "
    "backfilled. An hour the logger missed stays missing."
)
MATCHER_NOTE = (
    f"Rows before {MATCHER_CHANGED_ON} were scored with substring headline "
    f"matching (matcher null); later rows with whole-word matching (matcher "
    f"\"{MATCHER_VERSION}\"). Drivers (the top headlines behind a reading) are "
    f"stored from {MATCHER_CHANGED_ON} on; older rows have drivers null. "
    "See /validation for the methodology notes."
)
WINDOW_NOTE = (
    f"average_compound depends on the row's window column. window "
    f"\"{WINDOW_VERSION}\" (from {WINDOW_CHANGED_ON}): the recency-weighted score over "
    f"headlines from the last {MAX_AGE_HOURS} hours (weight halves every "
    f"{HALF_LIFE_HOURS} hours), the same number the live API returns; "
    "unweighted_compound_72h, effective_sample_size and newest_headline_age_hours "
    "are filled in. window null (earlier rows): the plain average over every item "
    "the feeds held, however old, and those four columns are null."
)


def _parse_ts(value: str) -> datetime:
    dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _hour(dt: datetime) -> datetime:
    return dt.replace(minute=0, second=0, microsecond=0)


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _drivers(raw: Optional[str]):
    """Stored drivers JSON as a list; None for rows that have none (written
    before the column existed) or unreadable JSON."""
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    return value if isinstance(value, list) else None


def _gaps(hours: set, first: datetime, last: datetime) -> list:
    """Missing hours between first and last (inclusive) as ranges."""
    gaps, start, h = [], None, first
    while h <= last:
        if h not in hours:
            start = start or h
        elif start:
            gaps.append((start, h - HOUR))
            start = None
        h += HOUR
    if start:
        gaps.append((start, last))
    return [
        {"from": _iso(a), "to": _iso(b), "hours": int((b - a) / HOUR) + 1} for a, b in gaps
    ]


def archive_summary(now: Optional[datetime] = None) -> dict:
    """What the archive holds, per symbol. Missing hours run from a symbol's
    first reading to the last hour it should have by now: the current hour
    once the grace period is over, for symbols still in HOURLY_SYMBOLS; its
    last reading for symbols no longer logged."""
    now = now or datetime.now(timezone.utc)
    latest_due = _hour(now) if now - _hour(now) >= _GRACE else _hour(now) - HOUR

    with _db() as conn:
        rows = conn.execute(
            'SELECT symbol, observed_at, matcher, drivers, "window" FROM sentiment_hourly ORDER BY observed_at'
        ).fetchall()

    per_symbol: dict = {}
    for symbol, observed_at, matcher, drivers, window in rows:
        entry = per_symbol.setdefault(
            symbol,
            {"hours": set(), "rows": 0, "whole_word": 0, "with_drivers": 0, "windowed": 0, "first": None, "last": None},
        )
        entry["windowed"] += window == WINDOW_VERSION
        observed = _parse_ts(observed_at)
        entry["hours"].add(_hour(observed))
        entry["rows"] += 1
        entry["whole_word"] += matcher == MATCHER_VERSION
        entry["with_drivers"] += bool(_drivers(drivers))
        entry["first"] = entry["first"] or observed
        entry["last"] = observed

    symbols = {}
    for symbol in sorted(per_symbol, key=lambda s: (s not in HOURLY_SYMBOLS, s)):
        e = per_symbol[symbol]
        logging_now = symbol in HOURLY_SYMBOLS
        end = max(latest_due, _hour(e["last"])) if logging_now else _hour(e["last"])
        missing = _gaps(e["hours"], _hour(e["first"]), end)
        symbols[symbol] = {
            "logging_now": logging_now,
            "first_reading": _iso(e["first"]),
            "latest_reading": _iso(e["last"]),
            "rows": e["rows"],
            "whole_word_rows": e["whole_word"],
            "rows_with_drivers": e["with_drivers"],
            "window_rows": e["windowed"],
            "missing_hours": sum(g["hours"] for g in missing),
            "missing": missing,
        }

    return {
        "what": "Hourly news-sentiment readings per symbol: average_compound (-1 to 1), "
        "sample_size, Fear & Greed value, matcher, the top 3 driving headlines and, "
        "from 2026-10-04, the unweighted 72-hour score, effective sample size, "
        "newest-headline age and window version.",
        "recording": RECORDING_NOTE,
        "methodology": MATCHER_NOTE,
        "average_compound_note": WINDOW_NOTE,
        "symbols_logged_now": list(HOURLY_SYMBOLS),
        "symbols": symbols,
        "total_rows": sum(s["rows"] for s in symbols.values()),
        "get_readings": {
            "endpoint": "GET /history/{symbol}?start=<ISO 8601>&end=<ISO 8601>",
            "price_usd": HISTORY_PRICE_USD,
            "payment": "x402, USDC on Base",
            "default_range": "last 7 days",
            "max_range": "30 days per call",
        },
        "bulk": "Plans with dataset access get every hourly row in GET /dataset/export (see /dataset/info).",
        "as_of": _iso(now.replace(microsecond=0)),
    }


class RangeError(ValueError):
    pass


def resolve_range(start: Optional[str], end: Optional[str], now: Optional[datetime] = None):
    """(start, end) as UTC datetimes: default the last 7 days, at most 30."""
    now = now or datetime.now(timezone.utc)
    try:
        end_dt = _parse_ts(end) if end else now
        start_dt = _parse_ts(start) if start else end_dt - DEFAULT_RANGE
    except ValueError as e:
        raise RangeError(f"start and end must be ISO 8601 times, e.g. 2026-10-01T00:00:00Z ({e})")
    if start_dt >= end_dt:
        raise RangeError("start must be before end")
    if end_dt - start_dt > MAX_RANGE:
        raise RangeError("The range can be at most 30 days per call; split it into several calls")
    return start_dt, end_dt


def symbol_in_archive(symbol: str) -> bool:
    with _db() as conn:
        return conn.execute("SELECT 1 FROM sentiment_hourly WHERE symbol = ? LIMIT 1", (symbol,)).fetchone() is not None


def _row_dict(row) -> dict:
    observed_at, compound, sample_size, fng, matcher, drivers, unweighted, effective, newest_age, window = row
    return {
        "observed_at": observed_at,
        "average_compound": compound,
        "sample_size": sample_size,
        "fear_greed_value": fng,
        "matcher": matcher,
        "drivers": _drivers(drivers),
        "unweighted_compound_72h": unweighted,
        "effective_sample_size": effective,
        "newest_headline_age_hours": newest_age,
        "window": window,
    }


def hourly_rows(symbol: Optional[str] = None, start: Optional[datetime] = None, end: Optional[datetime] = None) -> list:
    """Rows in observed_at order, drivers parsed; symbol included when not filtered."""
    sql = (
        "SELECT symbol, observed_at, average_compound, sample_size, fear_greed_value, matcher, drivers, "
        'unweighted_compound_72h, effective_sample_size, newest_headline_age_hours, "window" '
        "FROM sentiment_hourly WHERE 1=1"
    )
    params: list = []
    if symbol:
        sql += " AND symbol = ?"
        params.append(symbol)
    with _db() as conn:
        rows = conn.execute(sql + " ORDER BY observed_at, symbol", params).fetchall()
    out = []
    for row in rows:
        observed = _parse_ts(row[1])
        if (start and observed < start) or (end and observed >= end):
            continue
        entry = _row_dict(row[1:])
        if not symbol:
            entry = {"symbol": row[0], **entry}
        out.append(entry)
    return out


def history_payload(symbol: str, start: datetime, end: datetime) -> dict:
    rows = hourly_rows(symbol, start, end)
    return {
        "symbol": symbol,
        "start": _iso(start),
        "end": _iso(end),
        "count": len(rows),
        "rows": rows,
        "recording": RECORDING_NOTE,
        "methodology": MATCHER_NOTE,
        "average_compound_note": WINDOW_NOTE,
    }


HISTORY_EXAMPLE = {
    "symbol": "BTC",
    "start": "2026-10-04T00:00:00Z",
    "end": "2026-10-04T02:00:00Z",
    "count": 2,
    "rows": [
        {
            "observed_at": "2026-10-04T00:00:02.513104+00:00",
            "average_compound": 0.1123,
            "sample_size": 52,
            "fear_greed_value": 41,
            "matcher": "whole_word",
            "drivers": None,
            "unweighted_compound_72h": None,
            "effective_sample_size": None,
            "newest_headline_age_hours": None,
            "window": None,
        },
        {
            "observed_at": "2026-10-04T01:00:02.209871+00:00",
            "average_compound": 0.1388,
            "sample_size": 55,
            "fear_greed_value": 41,
            "matcher": "whole_word",
            "drivers": [
                {
                    "title": "Bitcoin ETFs log fifth straight day of inflows",
                    "source": "CoinDesk",
                    "link": "https://example.com/news/bitcoin-etf-inflows",
                    "published": "2026-10-03T22:05:00Z",
                    "score": 0.6249,
                    "weight": 0.8963,
                    "effect": 0.0221,
                }
            ],
            "unweighted_compound_72h": 0.1012,
            "effective_sample_size": 31.7,
            "newest_headline_age_hours": 1.2,
            "window": WINDOW_VERSION,
        },
    ],
    "recording": RECORDING_NOTE,
    "methodology": MATCHER_NOTE,
    "average_compound_note": WINDOW_NOTE,
}


def render_archive_html(summary: dict) -> str:
    """Self-contained page; every fact comes from archive_summary()."""
    rows = []
    for symbol, s in summary["symbols"].items():
        missing = (
            ", ".join(
                escape(g["from"]) if g["hours"] == 1 else f"{escape(g['from'])} to {escape(g['to'])}"
                for g in s["missing"][:10]
            )
            + (f" and {len(s['missing']) - 10} more" if len(s["missing"]) > 10 else "")
        ) or "none"
        rows.append(
            f"<tr><td><strong>{escape(symbol)}</strong>{'' if s['logging_now'] else ' (stopped)'}</td>"
            f"<td>{escape(s['first_reading'])}</td><td>{s['rows']}</td>"
            f"<td>{s['missing_hours']}</td><td class=\"small\">{missing}</td></tr>"
        )
    table = "".join(rows) or '<tr><td colspan="5">No readings recorded yet.</td></tr>'
    get = summary["get_readings"]
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Hourly Sentiment Archive</title>
<meta name="description" content="What's in the hourly crypto news-sentiment archive: symbols, first reading, row counts and missing hours.">
<style>
:root{{--bg:#0d1117;--panel:#161b22;--border:#30363d;--text:#e6edf3;--muted:#8b949e;--accent:#58a6ff}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--text);font:16px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}}
main{{max-width:860px;margin:0 auto;padding:40px 16px}}
a{{color:var(--accent)}}
.muted,.small{{color:var(--muted)}}.small{{font-size:.85rem}}
.scroll{{overflow-x:auto}}
table{{width:100%;border-collapse:collapse;font-size:.92rem}}
th,td{{text-align:left;padding:8px;border-bottom:1px solid var(--border);vertical-align:top}}
th{{color:var(--muted);font-weight:600}}
code{{background:var(--panel);padding:1px 5px;border-radius:4px;overflow-wrap:anywhere}}
</style></head>
<body><main>
<p><a href="/">Crypto Sentiment API</a></p>
<h1>Hourly sentiment archive</h1>
<p>{escape(summary["what"])}</p>
<p><strong>{escape(summary["recording"])}</strong></p>
<div class="scroll"><table>
<tr><th>Symbol</th><th>First reading (UTC)</th><th>Rows</th><th>Missing hours</th><th>Which hours</th></tr>
{table}
</table></div>
<p class="muted">{escape(summary["methodology"])}</p>
<p class="muted">{escape(summary["average_compound_note"])}</p>
<h2>Get the readings</h2>
<p><code>{escape(get["endpoint"])}</code>: {escape(get["price_usd"])} per call, paid with x402 in USDC on Base.
Default range is the {escape(get["default_range"])}, up to {escape(get["max_range"])}.</p>
<p>{escape(summary["bulk"])}</p>
<p class="muted">As of {escape(summary["as_of"])}. JSON: request this page without <code>Accept: text/html</code>.</p>
</main></body></html>"""
