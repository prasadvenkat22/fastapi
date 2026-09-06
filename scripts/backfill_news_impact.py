"""Label every stored headline with the price move that followed it.

Turns market_news_vectors from "what was said" into a dataset you can test a
sentiment scheme against. For each headline that names a tracked symbol it
writes one news_symbol_impact row carrying the symbol's spot and ATR14 on the
publication date and its 1- and 5-day forward returns, in percent AND in ATR
units.

WHY ATR UNITS. A 3% move in NVDA (ATR 3.3% of price) is a one-day event; the
same 3% in SNDK (ATR 6.3%) is half a normal day. A study that pools names on
percent returns is measuring which names are volatile, not which headlines
moved anything. Dividing by the ATR at publication is what makes the rows
comparable, and it is the same normalisation section 102 arrived at when it
started asking "how far, in that name's own daily moves".

WHAT IT DELIBERATELY DOES NOT DO. It does not classify sentiment. Labelling
outcomes and judging headlines are separate jobs and mixing them would mean
every re-scoring of the sentiment prompt required re-fetching prices. Run
this once; re-run the classifier as often as the prompt changes.

    python scripts/backfill_news_impact.py [--limit N] [--symbols SNDK,MU]
"""

import argparse
import os
import sys
import uuid
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import psycopg2
import yfinance as yf

from trading_engine.symbol_news import ALIASES


def _dsn() -> str:
    url = os.getenv("DATABASE_URL", "")
    return url.replace("postgresql+psycopg2://", "postgresql://").replace(
        "postgresql+asyncpg://", "postgresql://")


def load_prices(symbol: str):
    """Daily closes plus ATR14, indexed by date."""
    h = yf.Ticker(symbol).history(period="2y", interval="1d")
    if h.empty:
        return None
    h = h.copy()
    prev = h["Close"].shift(1)
    tr = (h["High"] - h["Low"]).combine((h["High"] - prev).abs(), max).combine(
        (h["Low"] - prev).abs(), max)
    h["atr14"] = tr.rolling(14).mean()
    h.index = [d.date() for d in h.index]
    return h


def forward(h, day, n):
    """Close n trading days after the first session on/after `day`."""
    days = [d for d in h.index if d >= day]
    if not days:
        return None, None, None
    i = h.index.get_loc(days[0])
    base = float(h["Close"].iloc[i])
    atr = h["atr14"].iloc[i]
    atr = float(atr) if atr == atr else None       # NaN check
    if i + n >= len(h):
        return base, atr, None
    return base, atr, float(h["Close"].iloc[i + n])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--symbols", default="")
    args = ap.parse_args()

    syms = ([s.strip().upper() for s in args.symbols.split(",") if s.strip()]
            or sorted(ALIASES))
    conn = psycopg2.connect(_dsn())
    conn.autocommit = True
    cur = conn.cursor()

    written = skipped = 0
    for sym in syms:
        pats = ALIASES.get(sym, [sym.lower()])
        clause = " OR ".join(["headline_text ILIKE %s"] * len(pats))
        sql = (
            "SELECT id, headline_text, source, "
            "(publication_date AT TIME ZONE 'America/New_York')::date "
            f"FROM market_news_vectors WHERE {clause} ORDER BY publication_date DESC"
        )
        if args.limit:
            sql += f" LIMIT {int(args.limit)}"
        cur.execute(sql, [f"%{p}%" for p in pats])
        rows = cur.fetchall()
        if not rows:
            print(f"{sym:6s} no headlines")
            continue

        prices = load_prices(sym)
        if prices is None:
            print(f"{sym:6s} no price history")
            continue

        n = 0
        for news_id, text, source, day in rows:
            spot, atr, c1 = forward(prices, day, 1)
            _, _, c5 = forward(prices, day, 5)
            if spot is None:
                skipped += 1
                continue
            r1 = (c1 / spot - 1) * 100 if c1 else None
            r5 = (c5 / spot - 1) * 100 if c5 else None
            m1 = (c1 - spot) / atr if (c1 and atr) else None
            m5 = (c5 - spot) / atr if (c5 and atr) else None
            cur.execute(
                """
                INSERT INTO news_symbol_impact
                  (id, news_id, symbol, headline_text, source, published_on,
                   spot_at_publish, atr14_at_publish, ret_1d_pct, ret_5d_pct,
                   move_1d_atr, move_5d_atr)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (news_id, symbol) DO UPDATE SET
                  ret_1d_pct = EXCLUDED.ret_1d_pct,
                  ret_5d_pct = EXCLUDED.ret_5d_pct,
                  move_1d_atr = EXCLUDED.move_1d_atr,
                  move_5d_atr = EXCLUDED.move_5d_atr
                """,
                (str(uuid.uuid4()), news_id, sym, text[:2000], source, day,
                 spot, atr, r1, r5, m1, m5),
            )
            n += 1
            written += 1
        print(f"{sym:6s} {n:4d} headlines labelled")

    print(f"\n{written} rows written, {skipped} skipped (no price on/after publication)")
    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
