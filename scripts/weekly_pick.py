"""Rank real weekly verticals on drift-corrected EV, priced at fills you could get.

Three things this does that a naive version does not, each because a naive
version produced a wrong answer on 2026-09-06:

  REALISTIC FILLS. Cost is ask(long) - bid(short), never mid-to-mid. The first
  run ranked a DELL 465/525 call spread top at a mid-to-mid 43.75 against 59.14
  of intrinsic -- buying $59 for $44, which does not exist. Its 465C bid was
  itself below intrinsic: a stale Friday close on an illiquid deep-ITM strike.

  A QUOTE-WIDTH FILTER. Open interest is not liquidity. The same run ranked a
  GOOGL 300/338 put spread second on a short leg quoted 0.03/0.08 -- a market
  91% of mid wide, where the mid is fiction. MAX_SPREAD_PCT rejects those.

  A PER-SYMBOL HORIZON. tk.options[0] is a different expiry for different
  names -- DELL's front week was 2026-09-11 and GOOGL's 2026-09-09 -- and the
  first run scored every candidate over a hardcoded 4 days. The forward-return
  window is now derived from each name's own expiry.

And the headline number is DEMEANED EV. Section 106: on SNDK the raw empirical
EV was +1192 a contract and 109% of it was drift, so the same structure at the
same price lost money once the trend was removed. Raw is printed beside it as
a reminder of how large the difference is, never as the ranking key.

    python scripts/weekly_pick.py --symbols NVDA,MRVL --side call
    python scripts/weekly_pick.py --symbols PANW --side put
"""

import argparse
import math
import os
import sys
from datetime import date, datetime

import numpy as np
import yfinance as yf

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trading_engine.greeks import leg_greeks, spread_greeks

# A market wider than this fraction of its own mid is not a price, it is an
# absence of one. 0.25 still admits wide weekly strikes; it rejects the 91%
# quote that ranked second on the first run.
MAX_SPREAD_PCT = float(os.getenv("PICK_MAX_SPREAD_PCT", "0.25"))
MIN_OI = int(os.getenv("PICK_MIN_OI", "50"))
# Demean against a BOUNDED history. Max history pulls 2008 and 2020 into a
# 2026 vol regime and flatters far-OTM tails; three years keeps the estimate
# in something resembling the present.
HISTORY = os.getenv("PICK_HISTORY", "3y")


def atr14(h):
    H, L, C = h["High"], h["Low"], h["Close"]
    pc = C.shift(1)
    tr = (H - L).combine((H - pc).abs(), max).combine((L - pc).abs(), max)
    return float(tr.rolling(14).mean().iloc[-1])


def rv20(h):
    r = np.diff(np.log(h["Close"].values[-21:]))
    return float(np.std(r, ddof=1) * math.sqrt(252)) if len(r) > 2 else float("nan")


def trading_days_to(exp: str) -> int:
    """Sessions from the next trading day to expiry inclusive, holidays aware."""
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


def usable(row):
    b, a = float(row["bid"]), float(row["ask"])
    if b <= 0 or a <= 0 or float(row.get("openInterest") or 0) < MIN_OI:
        return None
    mid = (a + b) / 2
    if mid <= 0 or (a - b) / mid > MAX_SPREAD_PCT:
        return None
    return b, a, mid, float(row.get("impliedVolatility") or 0)


def evaluate(sym, side):
    tk = yf.Ticker(sym)
    h = tk.history(period=HISTORY, interval="1d")
    if len(h) < 120:
        return [], None
    spot, a14, rv = float(h["Close"].iloc[-1]), atr14(h), rv20(h)
    exp = tk.options[0]
    fwd_days = trading_days_to(exp)
    c = h["Close"].values
    fwd = c[fwd_days:] / c[:-fwd_days] - 1.0
    dem = fwd - fwd.mean()
    chain = tk.option_chain(exp)
    calls = side == "call"
    df = chain.calls if calls else chain.puts

    q = {}
    for _, r in df.iterrows():
        u = usable(r)
        if u:
            q[float(r["strike"])] = u
    ks = sorted(q)
    ivs = [q[k][3] for k in ks if q[k][3] > 0]
    atm_iv = float(np.median(ivs)) if ivs else float("nan")
    out = []
    for lo in ks:
        for hi in ks:
            if hi <= lo:
                continue
            w = hi - lo
            if not (0.02 * spot <= w <= 0.12 * spot):
                continue
            if calls:                      # long lo, short hi -- buy ask, sell bid
                cost = q[lo][1] - q[hi][0]
                long_k, short_k, ivl, ivs_ = lo, hi, q[lo][3], q[hi][3]
                payoff = lambda p: np.clip(p - lo, 0, w) - cost
                room = (hi - spot) / a14   # OTM room above the short strike
            else:                          # PUT debit: long hi, short lo
                cost = q[hi][1] - q[lo][0]
                long_k, short_k, ivl, ivs_ = hi, lo, q[hi][3], q[lo][3]
                payoff = lambda p: np.clip(hi - p, 0, w) - cost
                room = (spot - lo) / a14
            if cost <= 0.05 or cost >= w:
                continue
            prices = spot * (1 + dem)
            dm = payoff(prices)
            raw = payoff(spot * (1 + fwd))
            # THE DECOMPOSITION, explicitly. EV is not P(win) x reward +
            # P(lose) x risk -- a vertical has a THIRD outcome, finishing
            # between the strikes, and for a deep-ITM structure that middle
            # band is where a large share of the probability sits. Ignoring
            # it overstates both tails. P comes from the name's own
            # drift-removed move distribution; the strikes decide where the
            # bands fall inside it, which is what ITM depth actually controls.
            if calls:
                p_max = float((prices >= hi).mean())
                p_min = float((prices <= lo).mean())
            else:
                p_max = float((prices <= lo).mean())
                p_min = float((prices >= hi).mean())
            p_mid = max(0.0, 1.0 - p_max - p_min)
            g = spread_greeks(spot, long_k, short_k, fwd_days / 252.0, ivl, ivs_,
                              call=calls)
            # DELTA AS PROBABILITY, beside the realised bands. A leg's delta
            # approximates P(that strike finishes in the money), so the SHORT
            # leg's delta is P(max profit) and the difference -- the net delta
            # people quote as "the probability of the trade" -- is actually
            # P(finishing BETWEEN the strikes), the partial band.
            #
            # Checked 2026-09-06 against three years of drift-removed moves:
            #   DELL 430/480  net delta 10.8% vs 9.9% realised   (0.9p out)
            #   NVDA 230/240  net delta 46.0% vs 39.1%           (6.9p)
            #   MRVL 220/235  net delta 32.5% vs 36.4%           (3.9p)
            # Good on the body, weakest on P(max) -- off by -6.0p and +5.2p --
            # which is exactly where the payoff lives. A sanity check on the
            # empirical numbers, not a replacement for them.
            dl = abs(leg_greeks(spot, long_k, fwd_days / 252.0, ivl, calls)["delta"])
            dh = abs(leg_greeks(spot, short_k, fwd_days / 252.0, ivs_, calls)["delta"])
            out.append(dict(
                sym=sym, lo=lo, hi=hi, w=w, cost=cost, spot=spot, atr=a14, rv=rv,
                iv=atm_iv, exp=exp, days=fwd_days,
                ev_dem=float(dm.mean()) * 100, ev_raw=float(raw.mean()) * 100,
                pwin=float((dm > 0).mean()), need=cost / w,
                rr=(w - cost) / cost, room=room, n=len(dem),
                p_max=p_max, p_mid=p_mid, p_min=p_min,
                d_long=dl, d_short=dh, d_net=dl - dh,
                # How deep the LONG leg sits, in the name's own ATR. THIS IS
                # THE KNOB: deeper ITM buys probability and sells payoff, and
                # they move against each other along a frontier rather than
                # one being simply better. In ATR, not dollars, so it means
                # the same on a 1740 stock and a 230 one.
                itm=((spot - lo) / a14) if calls else ((hi - spot) / a14),
                # EV as a percent of capital at risk. A dollar EV is not
                # comparable between a 258 risk and a 2090 one; this is.
                ev_pct=(float(dm.mean()) / cost * 100.0),
                **g))
    return out, dict(spot=spot, atr=a14, rv=rv, iv=atm_iv, exp=exp, days=fwd_days,
                     strikes=len(ks))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", required=True)
    ap.add_argument("--side", choices=("call", "put"), required=True)
    ap.add_argument("--top", type=int, default=5)
    ap.add_argument("--rr-min", type=float, default=0.0,
                    help="only structures paying at least this reward per unit "
                         "of risk; --rr-min 2 gives R:R 1:2 or better")
    ap.add_argument("--rr-max", type=float, default=0.0,
                    help="upper bound on reward per unit risk. Pair with "
                         "--rr-min to see a BAND: --rr-min 1.5 --rr-max 3 is "
                         "the moderate geometry, between the deep-ITM trap "
                         "(high probability, no payoff) and the OTM lottery "
                         "ticket (huge payoff, ~90% total loss).")
    ap.add_argument("--by", choices=("ev", "evpct", "prob"), default="evpct",
                    help="evpct = EV per dollar risked (default), prob = highest "
                         "probability of profit, ev = raw dollar EV")
    args = ap.parse_args()

    rows = []
    for s in [x.strip().upper() for x in args.symbols.split(",") if x.strip()]:
        try:
            r, meta = evaluate(s, args.side)
            if meta:
                ivrv = meta["iv"] / meta["rv"] if meta["rv"] else float("nan")
                print(f"{s:6s} spot {meta['spot']:8.2f}  ATR {meta['atr']:7.2f}  "
                      f"RV {meta['rv']*100:4.0f}%  IV {meta['iv']*100:4.0f}%  "
                      f"IV/RV {ivrv:4.2f}  exp {meta['exp']} ({meta['days']}d)  "
                      f"{meta['strikes']} usable strikes  {len(r)} candidates")
            rows += r
        except Exception as e:
            print(f"{s:6s} error {e}")

    if args.rr_min > 0:
        rows = [r for r in rows if r["rr"] >= args.rr_min]
    if args.rr_max > 0:
        rows = [r for r in rows if r["rr"] <= args.rr_max]
    if not rows:
        print("\nNothing passed the quote filter. That is a result, not a failure: "
              "on stale weekend marks it is the correct answer.")
        return
    label = {"ev": "DEMEANED EV ($)", "evpct": "EV PER $ RISKED",
             "prob": "PROBABILITY OF PROFIT"}[args.by]
    print(f"\n=== {args.side.upper()} DEBIT SPREADS — ranked by {label}, "
          f"priced at ask/bid ===")
    print(f"{'sym':6s} {'strikes':>14s} {'ITMatr':>7s} {'risk':>7s} {'reward':>7s} "
          f"{'R:R':>7s} {'dLong':>6s} {'dShrt':>6s} {'Pmax':>6s} {'Pmid':>6s} "
          f"{'Pmin':>6s} {'need':>6s} {'edge':>7s} {'EV%':>7s} {'EV$':>8s} "
          f"{'drift':>8s}")
    key = {"ev": lambda x: -x["ev_dem"], "evpct": lambda x: -x["ev_pct"],
           "prob": lambda x: -x["pwin"]}[args.by]
    for r in sorted(rows, key=key)[:args.top]:
        print(f"{r['sym']:6s} {r['lo']:6.0f}/{r['hi']:<7.0f} {r['itm']:+7.2f} "
              f"{r['cost']*100:7.0f} {(r['w']-r['cost'])*100:7.0f} "
              f"1:{r['rr']:<5.2f} {r['d_long']:6.2f} {r['d_short']:6.2f} "
              f"{r['p_max']*100:5.1f}% {r['p_mid']*100:5.1f}% "
              f"{r['p_min']*100:5.1f}% {r['need']*100:5.1f}% "
              f"{(r['pwin']-r['need'])*100:+6.1f}p {r['ev_pct']:+6.1f}% "
              f"{r['ev_dem']:+8.1f} {r['ev_raw'] - r['ev_dem']:+8.1f}")
    print("\nrisk/reward are per CONTRACT. need = cost/width = the break-even "
          "win rate. R:R sizes the WIN and says nothing about the ODDS, which "
          "is why a 1:5.78 payoff can still lose money.")
    print("EVdem is the forecast: what the structure earns if drift is "
          "unpredictable, which over two to five days it is. P(win) is "
          "measured on that same drift-removed history.")
    print("drift = EVraw - EVdem, what the name's past trend CONTRIBUTES. "
          "Large and positive means the raw number is mostly trend-following "
          "and will not survive a flat tape. NEGATIVE means the trade is "
          "fighting the drift and only pays if it stops. On SNDK that column "
          "was 109% of the raw figure, which is how section 106 caught it.")


if __name__ == "__main__":
    main()
