"""The two 0DTE structures worth placing today, sized to a budget.

    python scripts/dte0_pick.py --symbols NVDA --budget 1000
    python scripts/dte0_pick.py --symbols NVDA,MU,QQQ --budget 1000 --side put

IT PRINTS. IT DOES NOT TRADE. Every order path in this repository is either
the engine's own QQQ playbook or a manual decision, and a single-name 0DTE
book has no measured record here at all -- dte0_shadow starts collecting one
on 2026-09-14. This turns "what should I place" into a two-line answer without
also deciding to place it.

THE THREE CONSTRAINTS, each learned by losing money to its opposite this week.

    ENTRY 30-65% OF WIDTH. Above 65% the +30% take-profit is arithmetically
    unreachable -- max return is (width - entry)/entry, which at 0.77 of width
    is exactly 30%. Four QQQ positions on 2026-09-11 were bought at 0.68 to
    0.81 of width and their target rung silently did nothing; the 15:45
    flatten became the exit. Below 30% the opposite breaks: the premium is
    mostly time value, and the -10% stop is closer to entry than one morning
    of theta, so the clock stops you out of a position price never moved
    against.

    EXTRINSIC UNDER ~25% OF PREMIUM. The same failure stated in the units that
    cause it. NVDA 220/200 screened at +28 points of edge on 2026-09-12 with
    35% of its premium in time value: the stop sat 0.26 below entry against
    0.91 of extrinsic, so theta alone would have covered it before lunch.

    TARGET WITHIN ~0.3 ATR. The move needed to reach +30% has to be an
    ordinary session. ATR converts to a one-day sigma at ATR/1.596, the same
    constant the probability engines use, so this is expressed against the
    distribution the rest of the repo already measures against.

WHAT IT DOES NOT KNOW IS DIRECTION. It scores call and put structures
identically and prints the best of each. Which side to take is the morning's
question -- the news verdict, the flow read and the tape -- and pretending a
strike-selection rule answers it would be the same category error as the
+55% target that could never fire.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trading_engine.data_feed import fetch_option_chain, fetch_spot  # noqa: E402

# ATR -> one-day sigma. The same constant the probability engines use.
ATR_TO_SIGMA = 1.596
MIN_EW = float(os.getenv("TRADING_PICK_MIN_ENTRY_WIDTH", "0.30"))
MAX_EW = float(os.getenv("TRADING_PICK_MAX_ENTRY_WIDTH", "0.65"))
MAX_EXTRINSIC = float(os.getenv("TRADING_PICK_MAX_EXTRINSIC", "25.0"))
MAX_TARGET_ATR = float(os.getenv("TRADING_PICK_MAX_TARGET_ATR", "0.30"))
TARGET_PCT = float(os.getenv("TRADING_ORPHAN_TARGET_RETURN_PCT", "30.0"))
# The clamp every order passes through. Printing a size the broker will chop
# into pieces is how Friday's exit became four fills and $72 of slippage.
MAX_CONTRACTS = int(os.getenv("TRADING_MAX_ORDER_CONTRACTS", "5"))


def atr14(symbol: str) -> "float | None":
    """Gap-aware True Range over 14 sessions, from the same feed as everything else."""
    import httpx

    root = ("https://api.tradier.com/v1"
            if os.getenv("TRADIER_ENV", "sandbox").lower() == "production"
            else "https://sandbox.tradier.com/v1")
    try:
        r = httpx.get(root + "/markets/history",
                      params={"symbol": symbol, "interval": "daily",
                              "start": "2026-06-01", "end": date.today().isoformat()},
                      headers={"Authorization": f"Bearer {os.getenv('TRADIER_API_KEY')}",
                               "Accept": "application/json"}, timeout=25.0)
        rows = (r.json().get("history") or {}).get("day") or []
        if isinstance(rows, dict):
            rows = [rows]
    except Exception:
        return None
    if len(rows) < 15:
        return None
    trs = []
    for prev, cur in zip(rows[-15:-1], rows[-14:]):
        h, l, pc = float(cur["high"]), float(cur["low"]), float(prev["close"])
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs) / len(trs) if trs else None


def candidates(chain: dict, spot: float, atr: float, kind: str) -> list:
    """Every vertical that clears all three constraints, best first."""
    strikes = sorted({k for (t, k) in chain if t == kind})
    out = []
    for long_k in strikes:
        # The long leg sits at or just in the money -- a call below spot, a
        # put above it. Further out is the cheap structure the third
        # constraint exists to reject.
        if kind == "call" and not (spot - 10 <= long_k <= spot + 2):
            continue
        if kind == "put" and not (spot - 2 <= long_k <= spot + 10):
            continue
        for w in (2.5, 5.0, 7.5, 10.0, 15.0):
            short_k = long_k + w if kind == "call" else long_k - w
            lq, sq = chain.get((kind, long_k)), chain.get((kind, short_k))
            if not lq or not sq or lq.ask <= 0 or sq.bid <= 0:
                continue
            ask = round(lq.ask - sq.bid, 2)       # natural: what you pay
            if ask <= 0 or ask >= w:
                continue
            intr = (min(max(spot - long_k, 0.0), w) if kind == "call"
                    else min(max(long_k - spot, 0.0), w))
            extr = ask - intr
            # A negative extrinsic is a crossed or stale quote, not an
            # opportunity. Weekend marks produce them constantly.
            if extr < 0:
                continue
            ew = ask / w
            ex_pct = extr / ask * 100.0 if ask else 999.0
            tgt_mark = ask * (1 + TARGET_PCT / 100.0)
            if tgt_mark > w:
                continue
            tgt_spot = (long_k + tgt_mark) if kind == "call" else (long_k - tgt_mark)
            move = abs(tgt_spot - spot)
            if not (MIN_EW <= ew <= MAX_EW):
                continue
            if ex_pct > MAX_EXTRINSIC or move / atr > MAX_TARGET_ATR:
                continue
            out.append({
                "long": long_k, "short": short_k, "width": w, "ask": ask,
                "ew": ew, "extr_pct": ex_pct, "target_spot": tgt_spot,
                "move": move, "move_atr": move / atr,
                "max_ret": (w - ask) / ask * 100.0,
                "breakeven": (long_k + ask) if kind == "call" else (long_k - ask),
            })
    # Cheapest move to the target first; it is the only one of the three
    # constraints that is a matter of degree rather than a threshold.
    out.sort(key=lambda r: (r["move_atr"], -r["max_ret"]))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="NVDA")
    ap.add_argument("--budget", type=float, default=1000.0,
                    help="total dollars across the trades printed")
    ap.add_argument("--side", default="both", choices=("both", "call", "put"))
    ap.add_argument("--expiry", default="")
    args = ap.parse_args()

    syms = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    sides = ("call", "put") if args.side == "both" else (args.side,)
    per_trade = args.budget / max(len(sides), 1)
    print(f"budget ${args.budget:,.0f} over {len(sides)} trade(s) "
          f"= ${per_trade:,.0f} each   target +{TARGET_PCT:.0f}%   "
          f"order cap {MAX_CONTRACTS} contracts\n")

    for sym in syms:
        spot = float(fetch_spot(sym) or 0)
        atr = atr14(sym)
        exp = args.expiry or date.today().isoformat()
        chain = fetch_option_chain(exp, sym)
        if not spot or not atr or not chain:
            print(f"{sym}: no chain / spot / ATR for {exp} — skipped\n")
            continue
        print(f"{sym}  spot {spot:.2f}  ATR14 {atr:.2f} ({atr/spot*100:.1f}%)  "
              f"1-day sigma {atr/ATR_TO_SIGMA:.2f}  expiry {exp}")
        for kind in sides:
            rows = candidates(chain, spot, atr, kind)
            if not rows:
                print(f"   {kind.upper():5s} nothing clears the constraints — "
                      f"that is an answer, not a failure")
                continue
            r = rows[0]
            cost = r["ask"] * 100
            qty = max(0, min(int(per_trade // cost), MAX_CONTRACTS))
            if qty == 0:
                print(f"   {kind.upper():5s} best structure costs ${cost:,.0f}, "
                      f"above the ${per_trade:,.0f} per-trade budget")
                continue
            print(f"   {kind.upper():5s} {r['long']:.0f}/{r['short']:.0f} "
                  f"w{r['width']:.1f}  x{qty}  @ {r['ask']:.2f} "
                  f"= ${cost*qty:,.0f}")
            print(f"         entry {r['ew']:.0%} of width, extrinsic {r['extr_pct']:.0f}% "
                  f"of premium, max return +{r['max_ret']:.0f}%")
            print(f"         break-even {sym} {r['breakeven']:.2f}   "
                  f"+{TARGET_PCT:.0f}% at {sym} {r['target_spot']:.2f} "
                  f"({r['move']:.2f} = {r['move_atr']:.2f} ATR)   "
                  f"books ${r['ask']*TARGET_PCT/100*100*qty:,.0f}")
            if len(rows) > 1:
                a = rows[1]
                print(f"         next: {a['long']:.0f}/{a['short']:.0f} w{a['width']:.1f} "
                      f"@ {a['ask']:.2f}, target {a['move_atr']:.2f} ATR")
        print()

    print("PRINTS ONLY — nothing here places an order. Whatever you open is "
          "picked up by the orphan watcher on the next cycle:\n"
          "  stop -10% (intrinsic-guarded) | target +30% | trail 15% of band "
          "/ 5 min | flatten 15:45")


if __name__ == "__main__":
    main()
