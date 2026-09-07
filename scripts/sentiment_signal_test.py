"""Does the graded sentiment carry information about the forward return?

WHY THIS IS NOT xgb_probability.py --with-sentiment. That model has 11 features
and would be fitted on ~150 rows spanning ten sessions of fourteen correlated
tech names. It would memorise, and whatever test AUC fell out would be a number
with an error bar wider than the distance to a coin flip. Fitting a tree here
does not measure the thing; it hides it.

At this sample size the right test uses sentiment DIRECTLY as the score:

    AUC of (verdict ordinal x confidence) against P(forward return > 0)

One statistic, no fitting, no split to spend rows on, and a bootstrap interval
that says plainly whether 0.500 is inside it. If the signal cannot clear that,
no model built on top of it will either -- a tree cannot extract information
that is not in the column.

THE HORIZONS ARE REPORTED SEPARATELY. A 4-day target loses the last four
sessions, which at this sample is a third of the data, so the 1-day number is
computed on more rows and the 4-day on fewer. Neither is preferred; they are
both shown because choosing after seeing them is how a null result gets
talked into significance.

TIMESTAMPS. Rows stored before 2026-09-06 carry the INSERT time rather than the
publication time (section 123). For a CLOSE-to-close target that is safe: a
headline inserted during session D was knowable by D's close. It biases the
other way if anything -- an overnight story lands on the next morning's scrape,
which DELAYS information rather than leaking it.

    python scripts/sentiment_signal_test.py --backfill
    python scripts/sentiment_signal_test.py --horizons 1,4
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from datetime import date

import numpy as np
import psycopg2
import yfinance as yf

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trading_engine.symbol_news import ALIASES, classify_day, session_headlines

# Ordinal, not one-hot: the classes are ordered and there are not enough rows
# to spend a degree of freedom on each.
ORD = {"VERY_BEARISH": -2.0, "BEARISH": -1.0, "NEUTRAL": 0.0,
       "BULLISH": 1.0, "VERY_BULLISH": 2.0}


def _dsn() -> str:
    url = os.getenv("DATABASE_URL", "")
    return url.replace("postgresql+psycopg2://", "postgresql://").replace(
        "postgresql+asyncpg://", "postgresql://")


def news_days(cur):
    cur.execute("SELECT DISTINCT (publication_date AT TIME ZONE 'America/New_York')::date "
                "FROM market_news_vectors ORDER BY 1")
    return [r[0] for r in cur.fetchall()]


def backfill(cur, syms, days) -> int:
    """Grade every symbol-day that has headlines and does not yet have a row."""
    cur.execute("SELECT symbol, trading_day FROM news_verdicts")
    have = {(s, d) for s, d in cur.fetchall()}
    n = 0
    for s in syms:
        for d in days:
            if (s, d) in have:
                continue
            heads = session_headlines(s, d)
            if not heads:
                continue
            g = classify_day(s, d)
            cur.execute(
                "INSERT INTO news_verdicts (id, symbol, trading_day, verdict, "
                "confidence, rationale, headline_count) VALUES (%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (symbol, trading_day) DO NOTHING",
                (str(uuid.uuid4()), s, d, g["verdict"], g["confidence"],
                 g["rationale"], g["headline_count"]))
            n += 1
            print(f"  {s:6s} {d}  {g['verdict']:14s} {g['confidence']:.2f} "
                  f"({g['headline_count']} heads)")
    return n


def auc(scores: np.ndarray, labels: np.ndarray) -> float:
    """Rank AUC, ties counted at half. Written out rather than imported so the
    bootstrap below is obviously operating on the same statistic."""
    pos, neg = scores[labels == 1], scores[labels == 0]
    if len(pos) == 0 or len(neg) == 0:
        return float("nan")
    diff = pos[:, None] - neg[None, :]
    return float((diff > 0).mean() + 0.5 * (diff == 0).mean())


def bootstrap_by_day(sc, lb, dy, n=4000, seed=7):
    rng = np.random.default_rng(seed)
    udays = np.unique(dy)
    out = []
    for _ in range(n):
        pick = rng.choice(udays, size=len(udays), replace=True)
        idx = np.concatenate([np.flatnonzero(dy == d) for d in pick])
        a = auc(sc[idx], lb[idx])
        if a == a:
            out.append(a)
    return (float(np.percentile(out, 2.5)), float(np.percentile(out, 97.5))) if out \
        else (float("nan"), float("nan"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", action="store_true")
    ap.add_argument("--horizons", default="1,4")
    args = ap.parse_args()

    conn = psycopg2.connect(_dsn())
    conn.autocommit = True
    cur = conn.cursor()
    syms = sorted(ALIASES) + ["QQQ"]
    days = news_days(cur)

    if args.backfill:
        print("BACKFILLING VERDICTS (one model call per ungraded symbol-day)")
        print(f"  {len(syms)} symbols x {len(days)} days with headlines\n")
        print(f"\n{backfill(cur, syms, days)} verdicts written\n")

    cur.execute("SELECT symbol, trading_day, verdict, confidence, headline_count "
                "FROM news_verdicts ORDER BY trading_day, symbol")
    rows = cur.fetchall()
    print(f"{len(rows)} graded symbol-days available\n")
    if not rows:
        return

    px = {}
    for s in {r[0] for r in rows}:
        try:
            h = yf.Ticker(s).history(period="6mo", interval="1d")
            h.index = [d.date() for d in h.index]
            px[s] = h["Close"]
        except Exception as e:
            print(f"  {s}: price fetch failed, {e}")

    dist = {}
    for _, _, v, _, _ in rows:
        dist[v] = dist.get(v, 0) + 1
    print("verdict distribution: " + "  ".join(
        f"{k} {n}" for k, n in sorted(dist.items(), key=lambda x: -x[1])))

    for hz in (int(x) for x in args.horizons.split(",")):
        sc, lb, dy, rets = [], [], [], []
        for sym, day, v, conf, nh in rows:
            c = px.get(sym)
            if c is None:
                continue
            idx = sorted(c.index)
            if day not in idx:
                continue
            i = idx.index(day)
            if i + hz >= len(idx):
                continue
            r = float(c.loc[idx[i + hz]] / c.loc[day] - 1.0)
            sc.append(ORD.get(v, 0.0) * float(conf or 0.0))
            lb.append(1 if r > 0 else 0)
            dy.append(day.toordinal())
            rets.append(r)
        sc, lb, dy, rets = (np.array(x) for x in (sc, lb, dy, rets))
        print(f"\n{'='*72}\nHORIZON {hz} SESSION(S) — {len(sc)} rows, "
              f"{len(np.unique(dy))} distinct sessions\n{'='*72}")
        if len(sc) < 20 or len(np.unique(lb)) < 2:
            print("  too few rows or only one outcome class: not measurable")
            continue
        a = auc(sc, lb)
        lo, hi = bootstrap_by_day(sc, lb, dy)
        base = lb.mean()
        print(f"  base rate (up)          {base*100:5.1f}%")
        print(f"  AUC of sentiment alone  {a:.3f}   95% CI [{lo:.3f}, {hi:.3f}]"
              f"  (day-clustered bootstrap)")
        verdict = ("INSIDE the interval — indistinguishable from a coin flip"
                   if lo <= 0.5 <= hi else
                   "OUTSIDE the interval — sentiment separated the outcomes")
        print(f"  0.500 is {verdict}")
        for name, sel in (("bullish reads", sc > 0), ("bearish reads", sc < 0),
                          ("neutral reads", sc == 0)):
            if sel.sum():
                print(f"  {name:16s} n={int(sel.sum()):3d}  "
                      f"mean fwd {rets[sel].mean()*100:+6.2f}%  "
                      f"up {lb[sel].mean()*100:4.0f}%")

    print("\nAn interval this wide is the sample talking, not the signal. "
          "Ten sessions of correlated tech names cannot separate a real edge "
          "from noise, and the honest move is to keep accumulating rather than "
          "to read the point estimate.")
    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
