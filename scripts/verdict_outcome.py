"""Grade each 09:30 news verdict against the session it was written for.

    python scripts/verdict_outcome.py             # grade everything ungraded
    python scripts/verdict_outcome.py --report
    python scripts/verdict_outcome.py --report --era post-fix
    python scripts/verdict_outcome.py --regrade    # rebuild every row

IT MEASURES. IT DOES NOT TRADE, and nothing reads this table at runtime. The
verdict is advisory until this says otherwise, which is the whole point of
writing it down instead of arguing about it.

WHY IT EXISTS. news_watch.py carries the line "being in the news predicts
nothing", and that claim rests on news_symbol_impact -- 365 rows, all labelled
2026-09-06, with the sentiment column EMPTY on every one. What it measured is
whether a MENTION precedes a move (+0.086 ATR against a 1.178 standard
deviation: noise, as a mention should be). The graded verdict had never been
joined to an outcome, and the whole set predates the 2026-09-07 fixes.

THE HORIZON IS OPEN TO CLOSE. The verdict is written at 09:30 out of news
since the previous close, so the gap is already in the price by the time it
exists. Grading from the prior close would credit the read with a move it
could never have traded.

TWO NUMBERS ARE REPORTED AND THE SECOND ONE IS THE HONEST ONE.

    by row      every verdict counted once
    by session  averaged WITHIN a day first, then across days

Fifteen correlated tech names reading BEARISH on one morning and falling
together is ONE observation, not fifteen. The row count flatters every
result here, and the gap between the two columns is how much.

THE ERA SPLIT IS NOT COSMETIC. The window fix (news since the last close, not
since midnight), the macro-term balance and the novelty filter all landed on
2026-09-07. A verdict from before that date was produced by a pipeline reading
the wrong headlines, so pre- and post-fix rows are not the same measurement
and are never pooled in the summary.
"""

from __future__ import annotations

import argparse
import os
import statistics as st
import sys
from collections import defaultdict
from datetime import date

import httpx
import psycopg2

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# The day the window, macro-term and novelty fixes landed. Rows either side of
# it came from different pipelines and are reported separately.
FIX_DAY = date(2026, 9, 7)
ORDER = ["VERY_BULLISH", "BULLISH", "NEUTRAL", "BEARISH", "VERY_BEARISH"]
BULLISH = {"VERY_BULLISH", "BULLISH"}
BEARISH = {"VERY_BEARISH", "BEARISH"}


def _dsn() -> str:
    url = os.getenv("DATABASE_URL", "")
    return url.replace("postgresql+psycopg2://", "postgresql://").replace(
        "postgresql+asyncpg://", "postgresql://")


def _root() -> str:
    return ("https://api.tradier.com/v1"
            if os.getenv("TRADIER_ENV", "sandbox").lower() == "production"
            else "https://sandbox.tradier.com/v1")


_history: dict = {}


def daily(symbol: str) -> dict:
    """{date-string: bar} for one symbol, fetched once per run.

    Tradier rather than yfinance: the same source prices every other decision
    in this repository, and a verdict graded against a different vendor's bars
    would not be comparable with anything else measured here.
    """
    if symbol in _history:
        return _history[symbol]
    out: dict = {}
    try:
        r = httpx.get(
            _root() + "/markets/history",
            params={"symbol": symbol, "interval": "daily",
                    "start": "2026-06-01", "end": date.today().isoformat()},
            headers={"Authorization": f"Bearer {os.getenv('TRADIER_API_KEY')}",
                     "Accept": "application/json"},
            timeout=30.0,
        )
        if r.status_code == 200:
            rows = (r.json().get("history") or {}).get("day") or []
            if isinstance(rows, dict):
                rows = [rows]
            out = {row["date"]: row for row in rows}
    except Exception as exc:  # noqa: BLE001 — a missing symbol must not stop the run
        print(f"   {symbol}: history unavailable ({type(exc).__name__})")
    _history[symbol] = out
    return out


def atr14(bars: dict, day: str) -> "float | None":
    """Gap-aware True Range, averaged over the 14 sessions BEFORE `day`.

    Before, not including: the verdict is written at 09:30, so that session's
    own range is not knowable yet and must not scale the move it produced.
    """
    days = sorted(d for d in bars if d < day)
    if len(days) < 15:
        return None
    trs = []
    for prev, cur in zip(days[-15:-1], days[-14:]):
        h, l = float(bars[cur]["high"]), float(bars[cur]["low"])
        pc = float(bars[prev]["close"])
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs) / len(trs) if trs else None


def grade(regrade: bool = False) -> int:
    """Write an outcome row for every verdict that does not have one."""
    conn = psycopg2.connect(_dsn())
    conn.autocommit = True
    written = 0
    with conn, conn.cursor() as cur:
        if regrade:
            cur.execute("DELETE FROM news_verdict_outcomes")
        cur.execute("""
            SELECT v.symbol, v.trading_day, v.verdict, v.confidence, v.headline_count
            FROM news_verdicts v
            LEFT JOIN news_verdict_outcomes o
                   ON o.symbol = v.symbol AND o.trading_day = v.trading_day
            WHERE o.symbol IS NULL
            ORDER BY v.trading_day, v.symbol
        """)
        todo = cur.fetchall()
        print(f"{len(todo)} verdict(s) to grade")
        for symbol, day, verdict, conf, n in todo:
            bars = daily(symbol)
            bar = bars.get(day.isoformat())
            if not bar:
                continue
            o, c = float(bar["open"]), float(bar["close"])
            if o <= 0:
                continue
            ret = (c / o - 1.0) * 100.0
            atr = atr14(bars, day.isoformat())
            cur.execute("""
                INSERT INTO news_verdict_outcomes
                    (symbol, trading_day, verdict, confidence, headline_count,
                     open_px, close_px, ret_pct, atr14, move_atr, pipeline_era)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (symbol, trading_day) DO NOTHING
            """, (symbol, day, verdict, conf, n, o, c, ret, atr,
                  ((c - o) / atr) if atr else None,
                  "post-fix" if day >= FIX_DAY else "pre-fix"))
            written += 1
    conn.close()
    print(f"{written} graded")
    return written


def _stats(rows: list) -> tuple:
    """(n_rows, mean, median, n_sessions, session mean) for one bucket.

    The session figure averages within a trading day first. Without it the
    same market move is counted once per name that happened to be graded.
    """
    vals = [r["ret"] for r in rows]
    by_day: dict = defaultdict(list)
    for r in rows:
        by_day[r["day"]].append(r["ret"])
    day_means = [st.mean(v) for v in by_day.values()]
    return (len(vals), st.mean(vals), st.median(vals),
            len(day_means), st.mean(day_means))


def report(era: str = "") -> None:
    conn = psycopg2.connect(_dsn())
    with conn, conn.cursor() as cur:
        cur.execute("""
            SELECT verdict, trading_day, ret_pct, move_atr, pipeline_era
            FROM news_verdict_outcomes WHERE ret_pct IS NOT NULL
        """)
        rows = [{"verdict": v, "day": d, "ret": float(r),
                 "atr": float(m) if m is not None else None, "era": e}
                for v, d, r, m, e in cur.fetchall()]
    conn.close()

    if not rows:
        print("no graded rows yet — run without --report first")
        return

    eras = [era] if era else ["ALL", "pre-fix", "post-fix"]
    print(f"{len(rows)} graded verdicts, "
          f"{len({r['day'] for r in rows})} sessions, open -> close\n")
    for e in eras:
        sel = rows if e == "ALL" else [r for r in rows if r["era"] == e]
        if not sel:
            continue
        print(f"--- {e} ---")
        print(f"{'verdict':>14} {'rows':>5} {'mean':>8} {'median':>8} "
              f"{'right':>6}   {'sessions':>8} {'mean/session':>13}")
        for v in ORDER:
            bucket = [r for r in sel if r["verdict"] == v]
            if not bucket:
                continue
            n, mean, med, ns, smean = _stats(bucket)
            if v in BULLISH:
                right = sum(1 for r in bucket if r["ret"] > 0) / n * 100
            elif v in BEARISH:
                right = sum(1 for r in bucket if r["ret"] < 0) / n * 100
            else:
                right = None
            print(f"{v:>14} {n:5d} {mean:+7.3f}% {med:+7.3f}% "
                  f"{(f'{right:5.0f}%' if right is not None else '     -')}   "
                  f"{ns:8d} {smean:+12.3f}%")
        bull = [r for r in sel if r["verdict"] in BULLISH]
        bear = [r for r in sel if r["verdict"] in BEARISH]
        if bull and bear:
            spread = st.mean([r["ret"] for r in bull]) - st.mean([r["ret"] for r in bear])
            print(f"{'BULL - BEAR':>14} {len(bull) + len(bear):5d} {spread:+7.3f}%"
                  f"   <- the whole question, in one number")
        print()

    print("The session column is the honest one: correlated names reading the")
    print("same way on one morning are ONE observation, not fifteen.")
    print("ADVISORY. Nothing reads this table at runtime.")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--regrade", action="store_true",
                    help="delete and rebuild every outcome row")
    ap.add_argument("--era", default="", choices=("", "pre-fix", "post-fix"))
    args = ap.parse_args()

    if args.report:
        report(args.era)
        return
    grade(regrade=args.regrade)
    report(args.era)


if __name__ == "__main__":
    main()
