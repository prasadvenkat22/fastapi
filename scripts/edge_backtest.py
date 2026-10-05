"""Edge check: one-year backtest of the weekly book's DIRECTION vs a coin flip.

What the live book does: rank() (scripts/weekly_pick.py) prices every debit
vertical against the name's DEMEANED 3-year N-day return distribution, so the
ranker carries no directional forecast (direction only enters through chain
skew and the news weight). The directional content is the GATES. The one
computable from bars is the weekly VWAP gate (trading_engine/weekly_vwap_gate.py):
CALL only if spot is ABOVE the week-anchored VWAP by > 0.25 ATR and the VWAP is
rising, PUT only if BELOW and falling. That is the rule tested here. Chain
history does not exist, so the EV ranking itself cannot be replayed.

Bars: yfinance 60m (entry = close of the 09:30-10:30 bar, ~40 min after the
live 09:50 run), anchored VWAP from Monday's first bar; daily bars for ATR14 /
RV20. Calendar as live: Mon-Wed -> this Friday (3-day bucket), Thu-Fri -> next
Friday (7-day bucket). Settled at the expiry-date close.

Structure: long strike ATM (current W3/W7 bands straddle 0), width WIDTH_ATR x
daily ATR14. PRICE (assumption, stated): Black-Scholes, IV = RV20 x IVRV
(weekly_shadow rows: short_iv / rv20 median 0.97), r = 4%. COST: each leg pays
half its bid/ask, bid/ask = the name's logged "quote % of mid" (weekly-trade.log
averages), on entry and again on exit when the spread has value; $0.65/leg
commission each way. STX and PANW excluded (quote 19% > the 15% ceiling).
"""
from __future__ import annotations

import math
import sys
from datetime import timedelta

import numpy as np
import pandas as pd
import yfinance as yf
from scipy.stats import norm

QUOTE_PCT = dict(AVGO=3.3, AMD=3.1, MRVL=5.3, META=3.2, TSLA=1.8, AAPL=4.7, SNDK=2.9,
                 DELL=5.7, MU=2.3, NVDA=1.4, GOOGL=5.8, INTC=2.6, WDC=13.0, CRWV=4.5,
                 MSFT=6.4, AMZN=4.1)
SYMS = list(QUOTE_PCT)
WIDTH_ATR = float(sys.argv[1]) if len(sys.argv) > 1 else 1.5
IVRV = float(sys.argv[2]) if len(sys.argv) > 2 else 0.97
BAND_ATR = 0.25
R = 0.04
NRES = 2000
START = "2025-10-01"


def bs(S, K, T, vol, call):
    T = max(T, 1e-6)
    d1 = (math.log(S / K) + (R + 0.5 * vol * vol) * T) / (vol * math.sqrt(T))
    d2 = d1 - vol * math.sqrt(T)
    if call:
        return S * norm.cdf(d1) - K * math.exp(-R * T) * norm.cdf(d2)
    return K * math.exp(-R * T) * norm.cdf(-d2) - S * norm.cdf(-d1)


def build() -> pd.DataFrame:
    rows = []
    qqq = yf.Ticker("QQQ").history(period="2y", interval="1d", auto_adjust=False)
    qqq.index = qqq.index.tz_localize(None).normalize()
    qqq_sma20 = qqq.Close.rolling(20).mean()
    for sym in SYMS:
        d = yf.Ticker(sym).history(period="2y", interval="1d", auto_adjust=False)
        h = yf.Ticker(sym).history(period="730d", interval="60m", auto_adjust=False)
        if d.empty or h.empty:
            continue
        d.index = d.index.tz_localize(None).normalize()
        pc = d.Close.shift(1)
        tr = pd.concat([d.High - d.Low, (d.High - pc).abs(), (d.Low - pc).abs()], axis=1).max(axis=1)
        atr = tr.rolling(14).mean().shift(1)          # known before today's open
        rv = (np.log(d.Close).diff().rolling(20).std() * math.sqrt(252)).shift(1)
        h = h.tz_convert("America/New_York")
        h["day"] = h.index.tz_localize(None).normalize()
        h["wk"] = h["day"] - pd.to_timedelta(h["day"].dt.weekday, unit="D")
        tp = (h.High + h.Low + h.Close) / 3
        h["pv"] = tp * h.Volume
        h["cpv"] = h.groupby("wk").pv.cumsum()
        h["cv"] = h.groupby("wk").Volume.cumsum()
        h["vwap"] = h.cpv / h.cv
        h["vwap_prev"] = h.groupby("wk").vwap.shift(1)
        first = h[h.index.time == pd.Timestamp("09:30").time()]
        days = d.index
        for ts, b in first.iterrows():
            day = b.day
            if day < pd.Timestamp(START) or day not in atr.index:
                continue
            a, v = atr.get(day), rv.get(day)
            if not (a > 0 and v > 0):
                continue
            wd = day.weekday()
            fri = day + timedelta(days=4 - wd) if wd <= 2 else day + timedelta(days=11 - wd)
            exp_days = days[(days <= fri) & (days > day)]
            if len(exp_days) == 0 or exp_days[-1] < fri - timedelta(days=1):
                continue
            exp = exp_days[-1]
            spot = float(b.Close)
            slope = (b.vwap - b.vwap_prev) if not np.isnan(b.vwap_prev) else (b.Close - b.Open)
            if spot > b.vwap + BAND_ATR * a and slope > 0:
                rule = "call"
            elif spot < b.vwap - BAND_ATR * a and slope < 0:
                rule = "put"
            else:
                rule = None
            qd = qqq.index[qqq.index < day]
            q_up = bool(qqq.Close.loc[qd[-1]] > qqq_sma20.loc[qd[-1]]) if len(qd) else None
            rows.append(dict(sym=sym, day=day, exp=exp, bucket="W3" if wd <= 2 else "W7",
                             spot=spot, atr=float(a), rv=float(v), rule=rule, q_up=q_up,
                             vwap_dist=(spot - b.vwap) / a, close_exp=float(d.Close.loc[exp]),
                             tyrs=(int(((days > day) & (days <= exp)).sum()) + 0.85) / 252.0))  # sessions, RV is per-session
    return pd.DataFrame(rows)


def trade_ret(r, side) -> float:
    """Return on debit (incl. costs) for an ATM debit vertical on row r."""
    S, a, w = r.spot, r.atr, WIDTH_ATR * r.atr
    vol = r.rv * IVRV
    q = QUOTE_PCT[r.sym] / 100.0
    if side == "call":
        lo, hi = S, S + w
        pl, ps = bs(S, lo, r.tyrs, vol, True), bs(S, hi, r.tyrs, vol, True)
        val = min(max(r.close_exp - lo, 0.0), w)
    else:
        hi, lo = S, S - w
        pl, ps = bs(S, hi, r.tyrs, vol, False), bs(S, lo, r.tyrs, vol, False)
        val = min(max(hi - r.close_exp, 0.0), w)
    half = 0.5 * q * (pl + ps)
    debit = (pl - ps) + half + 0.013
    exit_cost = (half + 0.013) if val > 0.05 else 0.0
    return (val - exit_cost - debit) / debit


def main() -> None:
    df = build()
    df["ret_call"] = df.apply(lambda r: trade_ret(r, "call"), axis=1)
    df["ret_put"] = df.apply(lambda r: trade_ret(r, "put"), axis=1)
    days = sorted(df.day.unique())
    mid = days[len(days) // 2]
    df["half"] = np.where(df.day < mid, 1, 2)
    print(f"width {WIDTH_ATR} ATR, IV = RV20 x {IVRV}; {len(df)} symbol-days, "
          f"{df.sym.nunique()} names, {days[0].date()}..{days[-1].date()}, split at {mid.date()}")
    print(f"unconditional: always-call {df.ret_call.mean() * 100:+.1f}%/trade, "
          f"always-put {df.ret_put.mean() * 100:+.1f}%, coin flip "
          f"{(df.ret_call.mean() + df.ret_put.mean()) * 50:+.1f}%")

    rules = {
        "vwap gate (momentum)": lambda x: x.rule,
        "vwap contrarian": lambda x: {"call": "put", "put": "call"}.get(x.rule),
        "vwap gate + QQQ>20SMA agrees": lambda x: x.rule if x.rule and (x.rule == "call") == bool(x.q_up) else None,
        "QQQ trend only (all days)": lambda x: "call" if x.q_up else "put",
    }
    rng = np.random.default_rng(3)

    def evaluate(sub: pd.DataFrame, fn, label: str) -> float:
        side = sub.apply(fn, axis=1)
        m = side.notna()
        if m.sum() == 0:
            print(f"   {label}: no trades")
            return float("nan")
        s = sub[m]
        sd = side[m]
        act = np.where(sd == "call", s.ret_call, s.ret_put)
        rc, rp = s.ret_call.values, s.ret_put.values
        nul = np.array([np.where(rng.random(len(s)) < 0.5, rc, rp).mean() for _ in range(NRES)])
        pct = (nul < act.mean()).mean() * 100
        # Conservative null: the excess over the coin-flip mean, sign-flipped per
        # EXPIRY WEEK (trades sharing an expiry are one market move, not n draws).
        exc = pd.Series(act - (rc + rp) / 2, index=s.exp.values).groupby(level=0).sum()
        ev = exc.values
        flips = np.array([(ev * rng.choice((-1, 1), len(ev))).sum() for _ in range(NRES)])
        bpct = (flips < ev.sum()).mean() * 100
        # worst DAY: sum of returns across names entered that day (1 unit each)
        worst_day = pd.Series(act, index=s.day.values).groupby(level=0).sum().min()
        print(f"   {label:30s} n={m.sum():5d}  {act.mean() * 100:+6.1f}%/trade  win "
              f"{(act > 0).mean() * 100:4.1f}%  coin-flip {nul.mean() * 100:+6.1f}%  "
              f"pctile {pct:5.1f} (week-block {bpct:5.1f}, {len(ev)} wks)  worst day {worst_day:+.1f} units")
        return act.mean() - nul.mean()

    for bucket in ("W3", "W7"):
        b = df[df.bucket == bucket]
        for h in (1, 2):
            print(f"\n{bucket} half {h} ({b[b.half == h].day.min().date()}..{b[b.half == h].day.max().date()}):")
            for k, fn in rules.items():
                evaluate(b[b.half == h], fn, k)
    # Choose on half 1 (pooled buckets), test on half 2.
    for bucket in ("W3", "W7", None):
        b = df if bucket is None else df[df.bucket == bucket]
        print(f"\nSELECT ON HALF 1, TEST ON HALF 2 ({bucket or 'pooled'}):")
        best, best_ex = None, -1e9
        for k, fn in rules.items():
            ex = evaluate(b[b.half == 1], fn, "H1 " + k)
            if ex > best_ex:
                best, best_ex = k, ex
        print(f" chosen on H1 by excess over coin flip: {best}")
        evaluate(b[b.half == 2], rules[best], "H2 " + best)
    df.to_csv("scratch/edge/backtest_rows.csv", index=False)


if __name__ == "__main__":
    sys.exit(main())
