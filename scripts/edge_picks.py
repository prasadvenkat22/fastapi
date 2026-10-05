"""Edge check: the weekly book's actual picks vs null baselines, settled at expiry.

Picks are every engine order line in /var/log/weekly-trade.log (live since
2026-09-19). Order outcome from Tradier order_status: three 09-21 orders were
REJECTED, only TSLA 09-23 filled. All four are scored as picks (the selection
is what is being tested), and the filled one separately.

Null baselines on the same entry day/time (09:50 ET spot from yfinance 5m bars):
  A  random name from the symbol list, same direction
  B  same name, coin-flip direction (put mirrored at the same ATR moneyness)
  C  random name, coin-flip direction
Structure kept in the name's own daily ATR14: long-strike moneyness and width.
DEBIT ASSUMPTION: the null spread pays the SAME fraction of width as the pick
it replaces (the pick's logged natural debit / width). Same DTE, same ATR
geometry -> similar price fraction; skew and IV/ATR differences are ignored.
Settlement: expiry-date close (yfinance daily), payoff clip(.., 0, width).
"""
from __future__ import annotations

import sys

import numpy as np
import pandas as pd
import yfinance as yf

SYMS = ("AMZN,AVGO,CRWV,DELL,GOOGL,META,MRVL,MSFT,MU,NVDA,PANW,SNDK,STX,WDC,"
        "INTC,TSLA,AAPL,AMD").split(",")

# date, sym, side, long, short, qty, debit (logged natural), spot@09:50, expiry, filled
PICKS = [
    ("2026-09-21", "AMD", "call", 590.0, 635.0, 1, 11.85, 601.43, "2026-09-25", False),
    ("2026-09-21", "DELL", "put", 580.0, 527.5, 1, 15.32, 569.35, "2026-09-25", False),
    ("2026-09-21", "NVDA", "call", 220.0, 237.5, 3, 4.91, 224.41, "2026-09-25", False),
    ("2026-09-23", "TSLA", "call", 372.5, 417.5, 1, 13.33, 384.38, "2026-09-25", True),
]
N = 5000


from functools import lru_cache

_DAILY: dict = {}


@lru_cache(maxsize=None)
def _atr(sym: str, before: str) -> float:
    return atr14(_DAILY[sym], before)


def atr14(d: pd.DataFrame, before: str) -> float:
    h = d[d.index < before]
    pc = h.Close.shift(1)
    tr = pd.concat([h.High - h.Low, (h.High - pc).abs(), (h.Low - pc).abs()], axis=1).max(axis=1)
    return float(tr.rolling(14).mean().iloc[-1])


def payoff(side, spot_exp, lo, hi):
    w = hi - lo
    return np.clip(spot_exp - lo, 0, w) if side == "call" else np.clip(hi - spot_exp, 0, w)


def main() -> None:
    daily = {s: yf.Ticker(s).history(start="2026-07-01", end="2026-10-04", auto_adjust=False)
             for s in SYMS}
    for s in daily:
        daily[s].index = daily[s].index.strftime("%Y-%m-%d")
    _DAILY.update(daily)
    closes = {s: daily[s].Close.to_dict() for s in daily}
    m5 = yf.download(SYMS, start="2026-09-21", end="2026-09-24", interval="5m",
                     progress=False, auto_adjust=False)["Close"]
    m5.index = m5.index.tz_convert("America/New_York")

    @lru_cache(maxsize=None)
    def spot_at(sym, day):
        ts = pd.Timestamp(f"{day} 09:45", tz="America/New_York")
        return float(m5.loc[ts, sym])

    def geom(p):
        day, sym, side, long_k, short_k, qty, debit, spot, exp, _ = p
        a = atr14(daily[sym], day)
        w = abs(short_k - long_k)
        lo, hi = (long_k, short_k) if side == "call" else (short_k, long_k)
        m = (spot - long_k) / a if side == "call" else (long_k - spot) / a
        return dict(atr=a, w_atr=w / a, m=m, f=debit / w, day=day, exp=exp, side=side)

    def ret_of(sym, side, g):
        """Return on debit for the geometry g placed on sym/side."""
        a = _atr(sym, g["day"])
        s0 = spot_at(sym, g["day"])
        w = g["w_atr"] * a
        long_k = s0 - g["m"] * a if side == "call" else s0 + g["m"] * a
        lo, hi = (long_k, long_k + w) if side == "call" else (long_k - w, long_k)
        se = closes[sym][g["exp"]]
        debit = g["f"] * w
        return (payoff(side, se, lo, hi) - debit) / debit

    print("ACTUAL PICKS, settled at expiry close:")
    rows, G = [], []
    for p in PICKS:
        day, sym, side, long_k, short_k, qty, debit, spot, exp, filled = p
        lo, hi = (long_k, short_k) if side == "call" else (short_k, long_k)
        se = float(daily[sym].loc[exp, "Close"])
        val = float(payoff(side, se, lo, hi))
        g = geom(p)
        G.append(g)
        r = (val - debit) / debit
        rows.append(r)
        print(f"  {day} {sym:5s} {side:4s} {long_k}/{short_k} x{qty} @ {debit:.2f}  "
              f"exp close {se:.2f} value {val:.2f}  P&L ${(val - debit) * 100 * qty:+,.0f}  "
              f"ret {r * 100:+.0f}%  [long {g['m']:+.2f} ATR ITM, width {g['w_atr']:.2f} ATR, "
              f"debit {g['f'] * 100:.0f}% of width]{'  FILLED' if filled else '  (order rejected)'}")
    actual = float(np.mean(rows))
    print(f"  mean return on debit, 4 picks: {actual * 100:+.1f}%   filled TSLA only: {rows[3] * 100:+.0f}%")

    rng = np.random.default_rng(1)
    flip = {"call": "put", "put": "call"}
    nulls = {"A random name, same dir": [], "B same name, coin-flip dir": [],
             "C random name, coin-flip dir": []}
    for _ in range(N):
        a, b, c = [], [], []
        for p, g in zip(PICKS, G):
            sym, side = p[1], p[2]
            rs = SYMS[rng.integers(len(SYMS))]
            a.append(ret_of(rs, side, g))
            b.append(ret_of(sym, side if rng.random() < 0.5 else flip[side], g))
            rs2 = SYMS[rng.integers(len(SYMS))]
            c.append(ret_of(rs2, side if rng.random() < 0.5 else flip[side], g))
        nulls["A random name, same dir"].append(np.mean(a))
        nulls["B same name, coin-flip dir"].append(np.mean(b))
        nulls["C random name, coin-flip dir"].append(np.mean(c))
    print(f"\nNULLS ({N} resamples, mean return on debit over the same 4 slots):")
    for k, v in nulls.items():
        v = np.array(v)
        print(f"  {k:30s} null mean {v.mean() * 100:+6.1f}%  median {np.median(v) * 100:+6.1f}%  "
              f"actual percentile {(v < actual).mean() * 100 + (v == actual).mean() * 50:5.1f}")
    # Filled trade alone vs its own nulls.
    g = G[3]
    fa = np.array([ret_of(SYMS[rng.integers(len(SYMS))], "call", g) for _ in range(N)])
    print(f"  TSLA fill vs random names same dir: percentile {(fa < rows[3]).mean() * 100:.0f}, "
          f"null mean {fa.mean() * 100:+.0f}%")


if __name__ == "__main__":
    sys.exit(main())
