"""IV/RV screen across the tracked names, with the engine's own indicators.

IV/RV is the variance risk premium in one number:

    > 1.0   implied exceeds realised -> options are RICH  -> SELL premium
    < 1.0   implied below realised   -> options are CHEAP -> BUY premium

IT IS THE ONLY COLUMN HERE WITH MEASURED BACKING ON THIS BOOK. Section 50
established it: a credit spread's break-even win rate IS its risk ratio, delta
is the market's own probability estimate, and the only place an edge can come
from is implied exceeding realised. Trend, RSI, Bollinger and the VWAP side
are context printed beside it -- none has been validated against this book's
outcomes, and a name should not be traded because three unmeasured columns
agree.

RV20 comes from weekly_signals.read(), so it is the same annualised
close-to-close figure a weekly_shadow row carries rather than a parallel
calculation that could drift from it. IV is the median implied vol of
near-the-money calls on the front weekly expiry. Good enough to RANK names;
not a price for any specific spread -- use the chain for that.

    python scripts/iv_rv_screen.py [--symbols SNDK,MU]
"""

import argparse
import os
import sys

import numpy as np
import yfinance as yf

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import trading_engine.weekly_signals as ws
from trading_engine.symbol_news import ALIASES

# Thresholds are deliberately banded rather than a single 1.0 cut. A ratio of
# 1.01 is not a sell signal; it is noise around fair, and labelling it "SELL"
# would put a decision on the wrong side of a rounding error.
RICH, LEAN_RICH, LEAN_CHEAP, CHEAP = 1.25, 1.05, 0.95, 0.85


def atm_iv(sym: str, spot: float):
    """Median IV of calls within 10% of spot on the front expiry."""
    try:
        tk = yf.Ticker(sym)
        exp = tk.options[0]
        calls = tk.option_chain(exp).calls
        near = calls[(calls["strike"] > spot * 0.90) & (calls["strike"] < spot * 1.10)]
        ivs = [float(v) for v in near["impliedVolatility"] if v and float(v) > 0.01]
        return (float(np.median(ivs)) if ivs else None), exp
    except Exception:
        return None, None


def verdict_for(ratio):
    if ratio is None:
        return "no chain/vol"
    if ratio >= RICH:
        return "SELL premium"
    if ratio >= LEAN_RICH:
        return "lean SELL"
    if ratio <= CHEAP:
        return "BUY premium"
    if ratio <= LEAN_CHEAP:
        return "lean BUY"
    return "fair - no edge"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="")
    args = ap.parse_args()
    syms = ([s.strip().upper() for s in args.symbols.split(",") if s.strip()]
            or sorted(ALIASES))

    print(f"{'sym':6s} {'close':>9s} {'IV':>6s} {'RV20':>6s} {'IV/RV':>6s} "
          f"{'verdict':16s} {'trend':11s} {'RSI':>5s} {'vs wVWAP':>15s}")
    ranked = []
    for s in syms:
        try:
            d = ws.read(s)
            if not d:
                print(f"{s:6s}  no data")
                continue
            close, rv = d.get("close"), d.get("rv20")
            iv, _ = atm_iv(s, close)
            ratio = (iv / rv) if (iv and rv) else None
            v = verdict_for(ratio)
            vw, atr = d.get("vwap_week"), d.get("atr14")
            gap = ((close - vw) / atr) if (vw and atr) else None
            if ratio:
                ranked.append((ratio, s, v))
            print(f"{s:6s} {close:9.2f} {(iv or 0)*100:5.0f}% {(rv or 0)*100:5.0f}% "
                  f"{ratio or 0:6.2f} {v:16s} {str(d.get('trend')):11s} "
                  f"{d.get('rsi14') or 0:5.1f} {str(d.get('vwap_side')):>6s} "
                  f"{(f'{gap:+.2f} ATR' if gap is not None else ''):>8s}")
        except Exception as e:
            print(f"{s:6s}  error {e}")

    print("\nRANKED, richest options first (best to SELL premium):")
    for r, s, v in sorted(ranked, reverse=True):
        print(f"  {s:6s} IV/RV {r:5.2f}  {v}")
    print("\nA SCREEN, NOT A SIGNAL. It says where options look mispriced against "
          "recent realised vol. It does not say the direction holds, and nothing "
          "here has been validated end to end on this book.")


if __name__ == "__main__":
    main()
