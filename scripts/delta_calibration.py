"""Is the market's delta a well-calibrated probability, and where is it not?

THE MODEL THIS IS INSTEAD OF. The proposal was an XGBoost classifier feeding
predict_proba into the EV loop. That needs labelled outcomes and this book has
14 engine trades and zero days of per-symbol sentiment history -- a boosted
tree on that fits noise perfectly and reports a confident number for it.

Delta is already a probability estimate, supplied free by the market for every
strike, and on 2026-09-06 it tracked realised outcomes to within about a point
on the body of the distribution. So the useful question is not "can we replace
it" but "is it BIASED, and by how much, and where". That is a calibration
curve: one number per probability bucket, fitted from data we already hold.

METHOD. For every liquid strike on every tracked name, compare
    delta            the market's P(finish above this strike)
    realised         the same probability from the name's own drift-removed
                     N-day forward moves
bucketed by delta. A well-calibrated market puts realised on the diagonal.

READ THE LIMIT BEFORE THE RESULT. The realised side is one distribution per
name, resampled with overlapping windows, so strikes on the same name are not
independent observations. This measures whether delta is systematically off
across the names we trade, not how it behaves in general.

    python scripts/delta_calibration.py [--symbols NVDA,MRVL] [--side call]
"""

import argparse
import math
import os
import sys
from datetime import date

import numpy as np
import yfinance as yf

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trading_engine.greeks import leg_greeks

BUCKETS = ((0.0, 0.10), (0.10, 0.25), (0.25, 0.40), (0.40, 0.60),
           (0.60, 0.75), (0.75, 0.90), (0.90, 1.01))
DEFAULT_SYMS = "NVDA MRVL DELL MU SNDK CRWV PANW WDC AVGO".split()


def trading_days_to(exp: str) -> int:
    try:
        from trading_engine.market_calendar import is_trading_day
    except Exception:
        def is_trading_day(d):
            return d.weekday() < 5
    y, m, d = (int(x) for x in exp.split("-"))
    end, cur, n = date(y, m, d), date.today(), 0
    while cur <= end:
        if cur > date.today() and is_trading_day(cur):
            n += 1
        cur = date.fromordinal(cur.toordinal() + 1)
    return max(n, 1)


def collect(sym: str, side: str) -> list:
    tk = yf.Ticker(sym)
    h = tk.history(period="3y", interval="1d")
    if len(h) < 120:
        return []
    spot = float(h["Close"].iloc[-1])
    exp = tk.options[0]
    days = trading_days_to(exp)
    c = h["Close"].values
    # LOG returns, demeaned, then exponentiated. Demeaning SIMPLE returns and
    # applying them as spot*(1+r) sets the arithmetic mean to zero but leaves
    # the MEDIAN below spot by roughly 0.5*sigma^2*t -- volatility drag. Delta's
    # log-normal carries a -0.5*sigma^2*t term that centres the median, so the
    # two were not on the same footing. Measured 2026-09-07 that mismatch alone
    # produced an apparent +4 to +6.6 point "bias" on calls and -3 to -5 on
    # puts, in opposite directions, which is the signature of a method error
    # rather than a market one: a real volatility premium biases BOTH sides the
    # same way.
    lr = np.log(c[days:] / c[:-days])
    prices = spot * np.exp(lr - lr.mean())
    chain = tk.option_chain(exp)
    df = chain.calls if side == "call" else chain.puts
    calls = side == "call"
    out = []
    for _, r in df.iterrows():
        b, a = float(r["bid"]), float(r["ask"])
        oi = float(r.get("openInterest") or 0)
        iv = float(r.get("impliedVolatility") or 0)
        if b <= 0 or a <= 0 or oi < 50 or iv <= 0.01:
            continue
        mid = (a + b) / 2
        if (a - b) / mid > 0.25:
            continue
        k = float(r["strike"])
        d = abs(leg_greeks(spot, k, days / 252.0, iv, calls)["delta"])
        real = float((prices >= k).mean()) if calls else float((prices <= k).mean())
        out.append((sym, k, d, real))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="")
    ap.add_argument("--side", choices=("call", "put"), default="call")
    args = ap.parse_args()
    syms = ([s.strip().upper() for s in args.symbols.split(",") if s.strip()]
            or DEFAULT_SYMS)

    rows = []
    for s in syms:
        try:
            got = collect(s, args.side)
            rows += got
            print(f"{s:6s} {len(got):3d} liquid strikes")
        except Exception as e:
            print(f"{s:6s} error {e}")
    if not rows:
        print("\nno liquid strikes passed the filter")
        return

    print(f"\n{args.side.upper()} SIDE — delta vs realised, {len(rows)} strikes "
          f"across {len(syms)} names\n")
    print(f"{'delta bucket':16s} {'n':>4s} {'mean delta':>11s} {'realised':>10s} "
          f"{'bias':>8s}")
    biases = []
    for lo, hi in BUCKETS:
        sel = [r for r in rows if lo <= r[2] < hi]
        if len(sel) < 5:
            continue
        md = float(np.mean([r[2] for r in sel]))
        mr = float(np.mean([r[3] for r in sel]))
        biases.append((md, mr, len(sel)))
        print(f"{f'{lo:.2f}-{hi:.2f}':16s} {len(sel):4d} {md*100:10.1f}% "
              f"{mr*100:9.1f}% {(md-mr)*100:+7.1f}p")

    if biases:
        w = sum(n for _, _, n in biases)
        overall = sum((d - r) * n for d, r, n in biases) / w
        print(f"\nweighted mean bias  {overall*100:+.1f} points "
              f"(positive = delta OVERSTATES the probability)")
        print("\nA bias inside a couple of points is the market being right and "
              "leaves nothing to model. A large, MONOTONE bias across buckets "
              "is a calibration curve worth applying. A bias that flips sign "
              "between buckets is noise from overlapping windows, not signal.")


if __name__ == "__main__":
    main()
