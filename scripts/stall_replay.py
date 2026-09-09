"""Replay a give-back rule against a real session before trusting it live.

    python scripts/stall_replay.py --symbol SNDK --long 1700 --short 1850 \
        --entry 100.55 --qty 2 --day 2026-09-09 --giveback 5 --window 5

THE RULE UNDER TEST. "Sell if the position gives back N% of its MAX PROFIT
within W minutes of a high." Max profit is width minus entry, so N% of it is an
absolute dollar figure that does not change as the position moves -- which is
what makes it a different, and more stable, idea than a percentage of peak
value or a percentage of return.

WHY THIS SCRIPT EXISTS. Two thresholds have already been set on this account by
reasoning rather than measurement, and both were wrong:

    3.3 points of return   fired on the bid-ask breathing (0.03 ATR on SNDK)
    25  points of return   surrendered 64% of the position's whole profit band

Neither error was visible until the position was live. A rule that can place
orders should be replayed against a session that actually happened first, where
being wrong costs nothing.

WHAT IT MEASURES. Every fire, the price it would have sold at, and what that
did against simply holding to the session close. A rule that fires eleven times
in a session is not protecting a gain, it is trading the spread's noise.

THE SPREAD IS PRICED AT INTRINSIC, deliberately. Tradier's timesales gives the
UNDERLYING's bars, not the option's, so per-bar option marks do not exist to
replay. Intrinsic is the honest reconstruction: it is what orphans.py already
decides on (STALL_RESPECTS_INTRINSIC), it cannot be fooled by a wide quote, and
for a rule about the underlying moving it is the series that matters. The mark
would sit below it by the short leg's remaining time value.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

NY = ZoneInfo("America/New_York")


def bars(symbol: str, day: str, interval: str = "5min") -> list:
    key = os.getenv("TRADIER_API_KEY")
    env = os.getenv("TRADIER_ENV", "sandbox").lower()
    root = ("https://api.tradier.com/v1" if env == "production"
            else "https://sandbox.tradier.com/v1")
    r = httpx.get(root + "/markets/timesales",
                  params={"symbol": symbol, "interval": interval,
                          "start": f"{day} 09:30", "end": f"{day} 16:00",
                          "session_filter": "open"},
                  headers={"Authorization": f"Bearer {key}",
                           "Accept": "application/json"}, timeout=20.0)
    r.raise_for_status()
    d = (r.json().get("series") or {}).get("data") or []
    return [d] if isinstance(d, dict) else d


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--long", type=float, required=True)
    ap.add_argument("--short", type=float, required=True)
    ap.add_argument("--entry", type=float, required=True, help="net debit per spread")
    ap.add_argument("--qty", type=int, default=1)
    ap.add_argument("--day", default="")
    ap.add_argument("--giveback", type=float, default=5.0,
                    help="percent OF MAX PROFIT that triggers a sell")
    ap.add_argument("--window", type=float, default=5.0,
                    help="minutes: the give-back must happen within this long "
                         "of the high that preceded it")
    ap.add_argument("--interval", default="5min",
                    choices=("1min", "5min", "15min"))
    ap.add_argument("--profit-only", action="store_true",
                    help="ignore losing days entirely: only fire when the exit "
                         "would still book a gain against entry. This is the "
                         "difference between protecting a profit and stopping "
                         "a loss, and it changes the answer completely.")
    ap.add_argument("--from-time", default="",
                    help="ignore bars before this ET time, e.g. 09:45 if that "
                         "is when the position was actually opened")
    args = ap.parse_args()

    day = args.day or datetime.now(NY).strftime("%Y-%m-%d")
    width = abs(args.short - args.long)
    max_profit = width - args.entry
    trigger = max_profit * args.giveback / 100.0
    rows = bars(args.symbol, day, args.interval)
    if not rows:
        print("no bars")
        return

    print(f"{args.symbol} {args.long:.0f}/{args.short:.0f} x{args.qty}  "
          f"entry {args.entry:.2f}  width {width:.0f}")
    print(f"max profit {max_profit:.2f}/spread (${max_profit * args.qty * 100:,.0f})   "
          f"{args.giveback:.0f}% of it = {trigger:.2f} = "
          f"${trigger * args.qty * 100:,.0f} = {trigger:.2f} pts of {args.symbol}")
    print(f"window {args.window:.0f} min, priced at intrinsic, {args.interval} bars\n")

    def iv(px: float) -> float:
        return min(max(px - args.long, 0.0), width)

    peak = None
    peak_t = None
    fires = []
    first = last = None
    if args.profit_only:
        print(f"PROFIT ONLY: no fire unless intrinsic is above the "
              f"{args.entry:.2f} entry." + chr(10))
    print(f"  {'time':6s} {args.symbol:>9s} {'intrinsic':>10s} {'peak':>8s} "
          f"{'give':>7s} {'mins':>5s}")
    for b in rows:
        t = str(b.get("time", ""))[11:16]
        if args.from_time and t < args.from_time:
            continue
        c = float(b.get("close") or 0)
        if c <= 0:
            continue
        when = datetime.strptime(f"{day} {t}", "%Y-%m-%d %H:%M")
        v = iv(c)
        if first is None:
            first = (t, c, v)
        last = (t, c, v)
        if peak is None or v > peak:
            peak, peak_t = v, when
        give = peak - v
        mins = (when - peak_t).total_seconds() / 60.0
        in_profit = v > args.entry
        hit = (give >= trigger and mins <= args.window and mins > 0
               and (in_profit or not args.profit_only))
        flag = ("  <== SELL" if hit else
                ("  (giveback met, but at a LOSS — ignored)"
                 if (give >= trigger and mins <= args.window and mins > 0
                     and args.profit_only and not in_profit) else ""))
        print(f"  {t:6s} {c:9.2f} {v:10.2f} {peak:8.2f} {give:7.2f} "
              f"{mins:5.0f}{flag}")
        if hit:
            fires.append((t, c, v, give))
            peak, peak_t = v, when   # re-arm from here, as a live rule would

    print()
    if not fires:
        print("NEVER FIRED in this session.")
    else:
        print(f"FIRED {len(fires)} time(s):")
        for t, c, v, g in fires:
            print(f"   {t}  {args.symbol} {c:.2f}  intrinsic {v:.2f}  "
                  f"gave back {g:.2f}  -> would have sold for "
                  f"${(v - args.entry) * args.qty * 100:+,.0f} vs entry")
        f = fires[0]
        print(f"\nFIRST fire at {f[0]}: ${(f[2] - args.entry) * args.qty * 100:+,.0f}")
    if last:
        print(f"HOLDING to {last[0]} instead: "
              f"${(last[2] - args.entry) * args.qty * 100:+,.0f}  "
              f"({args.symbol} {last[1]:.2f}, intrinsic {last[2]:.2f})")
    print("\nA rule that fires many times in one session is trading noise, not "
          "protecting a gain. Compare the FIRST fire against holding: that is "
          "the whole question.")


if __name__ == "__main__":
    main()
