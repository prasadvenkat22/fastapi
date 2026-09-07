"""Would the morning's news have warned you off the trade you were about to put on?

THE WINDOW IS THE WHOLE POINT. Headlines are read from the PREVIOUS SESSION'S
CLOSE up to 09:30 on the day being graded. Everything in that window was
published before the opening bell and is therefore knowable to a trader
standing at the open with an order ticket -- and, just as importantly, not yet
traded on.

The earlier version of this script filtered by calendar date and read the whole
day's headlines. That was wrong twice over:

  1. it SKIPPED overnight and weekend catalysts. SanDisk's S&P 100 inclusion
     was stamped 2026-09-04 22:11 ET; a Monday "today only" filter never saw
     the single largest story on the book.
  2. it LEAKED. Grading a session's open using headlines written at 14:00 that
     same session is reading the tape and calling it a forecast, which is why
     same-day agreement used to look so convincing.

With the cutoff in place, BOTH columns below are forecasts:

  THIS session   open -> close of the day the news was published into. This is
                 the guard's actual question: "I am about to short SNDK at the
                 open, is that a bad idea?"
  NEXT session   open -> close of the following day, the swing-horizon claim.

    python scripts/news_backtest.py --days 2026-09-03,2026-09-04
    python scripts/news_backtest.py --days 2026-09-04 --cutoff 09:30
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, time as dtime

import yfinance as yf

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trading_engine.symbol_news import (classify_day, previous_session_close,
                                        session_headlines)

SYMS = "QQQ SNDK MU NVDA CRWV".split()
# A bullish verdict and a long structure agree; bullish and a short do not.
LONG_OK = ("VERY_BULLISH", "BULLISH")
SHORT_OK = ("VERY_BEARISH", "BEARISH")


def frames(syms):
    out = {}
    for s in syms:
        h = yf.Ticker(s).history(period="3mo", interval="1d")
        H, L, C = h["High"], h["Low"], h["Close"]
        pc = C.shift(1)
        tr = (H - L).combine((H - pc).abs(), max).combine((L - pc).abs(), max)
        h = h.copy()
        h["atr14"] = tr.rolling(14).mean()
        h.index = [d.date() for d in h.index]
        out[s] = h
    return out


def move(h, day, forward=0):
    days = sorted(h.index)
    if day not in days:
        return None
    i = days.index(day) + forward
    if i >= len(days):
        return None
    d = days[i]
    o, c = float(h.loc[d, "Open"]), float(h.loc[d, "Close"])
    atr = float(h.loc[d, "atr14"]) if h.loc[d, "atr14"] == h.loc[d, "atr14"] else None
    return dict(day=d, ret=(c / o - 1) * 100, atr=(c - o) / atr if atr else None)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", required=True, help="comma-separated YYYY-MM-DD")
    ap.add_argument("--symbols", default="")
    ap.add_argument("--cutoff", default="09:30",
                    help="stop reading headlines at this ET time on the day "
                         "being graded; anything later is lookahead")
    args = ap.parse_args()
    syms = ([s.strip().upper() for s in args.symbols.split(",") if s.strip()]
            or SYMS)
    days = [date(*(int(x) for x in d.strip().split("-")))
            for d in args.days.split(",") if d.strip()]
    hh, mm = (int(x) for x in args.cutoff.split(":"))
    cutoff = dtime(hh, mm)
    fr = frames(syms)

    calls = []  # (verdict, this-session return) for the scoreboard
    for d in days:
        start = previous_session_close(d)
        print(f"\n{'='*78}")
        print(f"{d} — headlines from {start:%a %Y-%m-%d %H:%M} ET to "
              f"{d:%Y-%m-%d} {args.cutoff} ET (knowable at the open)")
        print("="*78)
        print(f"{'sym':6s} {'verdict':14s} {'conf':>5s} {'n':>3s} "
              f"{'THIS session':>18s} {'NEXT session':>18s}   guard on a...")
        for s in syms:
            heads = session_headlines(s, d, cutoff)
            if not heads:
                print(f"{s:6s} {'(no news)':14s}   nothing published in the window")
                continue
            r = classify_day(s, d, cutoff)
            v = r["verdict"]
            this, nxt = move(fr[s], d, 0), move(fr[s], d, 1)
            td = (f"{this['ret']:+6.2f}% {this['atr']:+5.2f}atr" if this else "        -")
            nx = (f"{nxt['ret']:+6.2f}% {nxt['atr']:+5.2f}atr" if nxt else
                  "   (no session yet)")
            g = []
            if v in LONG_OK:
                g.append("SHORT = conflict")
            if v in SHORT_OK:
                g.append("LONG = conflict")
            print(f"{s:6s} {v:14s} {r['confidence']:5.2f} {r['headline_count']:3d} "
                  f"{td:>18s} {nx:>18s}   {', '.join(g) or 'neither'}")
            print(f"       {(r['rationale'] or '')[:116]}")
            if this:
                calls.append((v, this["ret"]))

    graded = [(v, x) for v, x in calls if v in LONG_OK + SHORT_OK]
    if graded:
        hit = sum(1 for v, x in graded
                  if (v in LONG_OK and x > 0) or (v in SHORT_OK and x < 0))
        print(f"\nDIRECTIONAL CALLS ON THE SESSION THE NEWS PRECEDED: "
              f"{hit}/{len(graded)} correct")
        print("A coin flip is the bar. At this sample size the number is an "
              "anecdote whichever way it lands -- it is here so it accumulates, "
              "not so it decides anything today.")
    elif calls:
        print("\nEvery verdict in the sample was NEUTRAL: nothing directional "
              "to score.")
    else:
        print("\nNo session has closed yet for these days, so nothing is "
              "scoreable. The verdicts above are the read a trader would carry "
              "into the open, which is the whole point of them.")

    print("\nBoth columns are now forecasts, because the cutoff makes them so. "
          "Drop the cutoff and the THIS-session column becomes a description "
          "of a move the headlines were written about.")


if __name__ == "__main__":
    main()
