"""
Lead/lag test: does hourly sentiment lead, coincide with, or lag price?

Reads sentiment_hourly (see app/hourly.py) from the sqlite DB, pulls hourly
candles from Coinbase's public API (no key), and computes the
cross-correlation between hourly CHANGES in average_compound and hourly log
returns at offsets -12h..+12h.

Offset convention (k, in hours):
  Δs_t  = sentiment(t) - sentiment(t-1h)   -- change over the hour ending at t
  R     = log return over the hour ending at t+k
  k > 0 : price move comes AFTER the sentiment change  -> sentiment LEADS
  k = 0 : same hour                                     -> coincident
  k < 0 : price move came BEFORE the sentiment change   -> sentiment LAGS

Changes, not levels: sentiment levels drift slowly and correlate with almost
anything trending; changes are the honest test.

Significance: a correlation is flagged only if |r| > z/sqrt(n) with a
Bonferroni-corrected z (25 offsets tested -> z ~= 3.09 for 5% overall).
The looser 1.96/sqrt(n) band is also shown for context.

Run on Render (Shell tab), where the DB lives:
    python scripts/leadlag.py                       # BTC, uses $BILLING_DB_PATH
    python scripts/leadlag.py --symbol ETH
    python scripts/leadlag.py --out app/validation.json   # writes page data
    python scripts/leadlag.py --whole-word-only     # only readings scored by
                                                    # the 2026-10-03 matcher
    python scripts/leadlag.py --since 2026-10-04T00:00:00Z

Readings before 2026-10-03 used substring headline matching (see
app/validation.json's methodology_changes). Rows written since record
`matcher = 'whole_word'`; --whole-word-only keeps just those, so the two
methods are never mixed in one window.

From 2026-10-04 rows also record `window = '72h-hl24'` (72-hour window,
weight halving every 24 hours). --window-only keeps just those rows, and
--score picks the column: "weighted" (average_compound, the API's score;
default) or "unweighted" (unweighted_compound_72h, only on window rows):
    python scripts/leadlag.py --window-only --score weighted

Offline / testing: --prices-csv file with rows "iso_hour_start,close".
Stdlib + httpx only (httpx is already in requirements.txt).
"""

import argparse
import csv
import json
import math
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

MAX_OFFSET = 12
N_OFFSETS = 2 * MAX_OFFSET + 1
Z_BONF = 3.09  # two-sided, alpha 0.05 / 25 offsets
Z_95 = 1.96
MIN_PAIRS = 48  # below this, refuse to call a verdict


def hour_floor(dt: datetime) -> datetime:
    return dt.replace(minute=0, second=0, microsecond=0)


def parse_ts(s: str) -> datetime:
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


WINDOW_VERSION = "72h-hl24"  # app/window.py's WINDOW_VERSION
SCORE_COLUMNS = {"weighted": "average_compound", "unweighted": "unweighted_compound_72h"}


def load_sentiment(db_path: str, symbol: str, since=None, whole_word_only=False,
                   window_only=False, score="weighted") -> dict:
    """{hour_start: score}. Readings land a few seconds after :00, so
    flooring to the hour gives the reading AT that hour boundary.

    since: drop readings observed before this datetime.
    whole_word_only: keep only rows scored by the whole-word matcher
    (matcher = 'whole_word'); a DB without that column has none.
    window_only: keep only rows from the current window version.
    score: "weighted" (average_compound) or "unweighted"
    (unweighted_compound_72h, which only window rows have)."""
    column = SCORE_COLUMNS[score]
    conn = sqlite3.connect(db_path)
    columns = {r[1] for r in conn.execute("PRAGMA table_info(sentiment_hourly)")}
    needed = {column}
    if whole_word_only:
        needed.add("matcher")
    if window_only:
        needed.add("window")
    if not needed <= columns:
        conn.close()
        return {}
    sql = (f"SELECT observed_at, {column} FROM sentiment_hourly "
           f"WHERE symbol = ? AND {column} IS NOT NULL")
    params = [symbol]
    if whole_word_only:
        sql += " AND matcher = 'whole_word'"
    if window_only:
        sql += ' AND "window" = ?'
        params.append(WINDOW_VERSION)
    rows = conn.execute(sql + " ORDER BY observed_at", params).fetchall()
    conn.close()
    out = {}
    for ts, val in rows:
        observed = parse_ts(ts)
        if since is not None and observed < since:
            continue
        h = hour_floor(observed)
        out.setdefault(h, float(val))  # keep first reading if duplicated
    return out


def fetch_coinbase_closes(symbol: str, start: datetime, end: datetime) -> dict:
    """{hour_start: close} for SYMBOL-USD. Coinbase caps 300 candles/request."""
    import httpx

    product = f"{symbol}-USD"
    out = {}
    cursor = start
    with httpx.Client(timeout=20, headers={"User-Agent": "crypto-sentiment-x402 leadlag"}) as client:
        while cursor < end:
            chunk_end = min(cursor + timedelta(hours=299), end)
            r = client.get(
                f"https://api.exchange.coinbase.com/products/{product}/candles",
                params={
                    "granularity": 3600,
                    "start": cursor.isoformat(),
                    "end": chunk_end.isoformat(),
                },
            )
            r.raise_for_status()
            for t, _low, _high, _open, close, _vol in r.json():
                out[datetime.fromtimestamp(t, tz=timezone.utc)] = float(close)
            cursor = chunk_end
    return out


def load_prices_csv(path: str) -> dict:
    out = {}
    with open(path) as f:
        for row in csv.reader(f):
            if row and not row[0].startswith("#"):
                out[hour_floor(parse_ts(row[0]))] = float(row[1])
    return out


def pearson(xs, ys):
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx == 0 or syy == 0:
        return None
    return sxy / math.sqrt(sxx * syy)


def analyze(sent: dict, closes: dict) -> dict:
    H = timedelta(hours=1)
    # Δs at hour boundary t (needs readings at t and t-1h)
    dsent = {t: sent[t] - sent[t - H] for t in sent if (t - H) in sent}
    # Return over the hour ENDING at boundary t = ln(close of candle starting
    # t-1h / close of candle starting t-2h). A candle starting at h closes at h+1h.
    ret = {}
    for h in closes:
        prev = h - H
        if prev in closes and closes[prev] > 0 and closes[h] > 0:
            ret[h + H] = math.log(closes[h] / closes[prev])

    results = []
    for k in range(-MAX_OFFSET, MAX_OFFSET + 1):
        xs, ys = [], []
        for t, d in dsent.items():
            r = ret.get(t + k * H)
            if r is not None:
                xs.append(d)
                ys.append(r)
        n = len(xs)
        corr = pearson(xs, ys)
        results.append({
            "offset_hours": k,
            "n": n,
            "corr": None if corr is None else round(corr, 4),
            "band_95": round(Z_95 / math.sqrt(n), 4) if n else None,
            "significant": bool(corr is not None and n and abs(corr) > Z_BONF / math.sqrt(n)),
        })

    n0 = next(r["n"] for r in results if r["offset_hours"] == 0)
    sig = [r for r in results if r["significant"]]
    if n0 < MIN_PAIRS:
        verdict = "insufficient_data"
        summary = f"Only {n0} paired hours; need at least {MIN_PAIRS} before calling it."
    elif not sig:
        verdict = "no_significant_relationship"
        summary = ("No offset clears the multiple-comparison-corrected threshold. "
                   "Sentiment changes show no measurable lead or lag vs price in this window.")
    else:
        best = max(sig, key=lambda r: abs(r["corr"]))
        k = best["offset_hours"]
        verdict = "leads" if k > 0 else "coincident" if k == 0 else "lags"
        when = (f"{k}h before price" if k > 0 else "in the same hour as price"
                if k == 0 else f"{-k}h after price")
        summary = (f"Strongest significant link: sentiment changes move {when} "
                   f"(r={best['corr']:+.3f}, n={best['n']}).")
    return {"verdict": verdict, "summary": summary, "offsets": results,
            "paired_hours": n0,
            "sentiment_hours": len(sent)}


def print_report(symbol, res, first, last):
    print(f"\n{symbol}: sentiment Δ vs {symbol}-USD hourly log return")
    print(f"window {first:%Y-%m-%d %H:00Z} -> {last:%Y-%m-%d %H:00Z}, "
          f"{res['sentiment_hours']} sentiment hours, {res['paired_hours']} paired at k=0\n")
    print(" k(h)    n     corr   ±95%   sig  (k>0 = sentiment leads)")
    for r in res["offsets"]:
        c = "   n/a" if r["corr"] is None else f"{r['corr']:+.3f}"
        bar = "" if r["corr"] is None else "#" * int(abs(r["corr"]) * 40)
        print(f"{r['offset_hours']:+4d} {r['n']:5d}  {c}  {r['band_95'] or 0:.3f}  "
              f"{'*' if r['significant'] else ' '}   {bar}")
    print(f"\nVERDICT: {res['verdict']}\n{res['summary']}\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.environ.get("BILLING_DB_PATH", "billing.db"))
    ap.add_argument("--symbol", default="BTC")
    ap.add_argument("--prices-csv", help="offline prices: iso_hour_start,close")
    ap.add_argument("--out", help="write/merge result JSON (e.g. app/validation.json)")
    ap.add_argument("--since", type=parse_ts, help="only readings observed at or after this ISO time")
    ap.add_argument("--whole-word-only", action="store_true",
                    help="only readings scored by the whole-word matcher (2026-10-03 on)")
    ap.add_argument("--window-only", action="store_true",
                    help=f"only readings from window version {WINDOW_VERSION} (2026-10-04 on)")
    ap.add_argument("--score", choices=sorted(SCORE_COLUMNS), default="weighted",
                    help="weighted = average_compound (default); unweighted = unweighted_compound_72h")
    a = ap.parse_args()
    sym = a.symbol.upper()

    sent = load_sentiment(a.db, sym, since=a.since, whole_word_only=a.whole_word_only,
                          window_only=a.window_only, score=a.score)
    if len(sent) < 2:
        which = (" window" if a.window_only else "") + (" whole-word" if a.whole_word_only else "")
        sys.exit(f"Only {len(sent)}{which} hourly rows for {sym} in {a.db}; nothing to analyze yet.")
    first, last = min(sent), max(sent)
    pad = timedelta(hours=MAX_OFFSET + 2)
    closes = (load_prices_csv(a.prices_csv) if a.prices_csv
              else fetch_coinbase_closes(sym, first - pad, last + pad))

    res = analyze(sent, closes)
    print_report(sym, res, first, last)

    if a.out:
        doc = {}
        if os.path.exists(a.out):
            with open(a.out) as f:
                doc = json.load(f)
        doc.setdefault("symbols", {})[sym] = {
            "status": "measured" if res["verdict"] != "insufficient_data" else "measuring",
            "verdict": res["verdict"],
            "summary": res["summary"],
            "window_start": first.isoformat(),
            "window_end": last.isoformat(),
            "paired_hours": res["paired_hours"],
            "readings": ", ".join(filter(None, [
                f"window {WINDOW_VERSION} only" if a.window_only else "",
                "whole-word matcher only" if a.whole_word_only else "",
            ])) or "all rows",
            "score": f"{a.score} ({SCORE_COLUMNS[a.score]})",
            "offsets": res["offsets"],
        }
        doc["updated"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        with open(a.out, "w") as f:
            json.dump(doc, f, indent=2)
        print(f"wrote {a.out}")


if __name__ == "__main__":
    main()
