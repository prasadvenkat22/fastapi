"""One-shot operator trigger on the UNDERLYING, not the spread mark.

    python scripts/underlying_trigger.py SYMBOL LONG_STRIKE SHORT_STRIKE FLOOR \
        [--cushion POINTS] [--shade POINTS] [--poll SECONDS]

Closes the named debit spread the moment the share price crosses the trigger
level: BELOW it for a call spread (long < short), ABOVE it for a put spread
(long > short). Fires once, through orphans._close (so the holdings clamp
applies), then exits. Does NOT re-arm on a recovery. Kill: pkill -f
underlying_trigger. Log: /app/underlying_trigger_SYMBOL.log (host: /opt/fastapi/).

WHY THE UNDERLYING. The spread mark on an illiquid name is a lagging, noisy
read of the share price through two option quotes -- on 2026-09-18 SNDK's mark
sat 16 points under intrinsic while the stock was pinned above the short
strike. The share price against the short strike is the one number that
decides whether the pin holds, so that is the number this watches.

THE LEVEL TRAILS. A fixed FLOOR protects against the worst case and nothing
else: QQQ 715/717 sat at 716.7 with a 715.50 floor, and a climb to 717.4
followed by a slide back to 715.6 would have handed back every cent of the
climb without the floor moving. With --cushion the level ratchets:

    call:  level = max(FLOOR, min(high_water - cushion, SHORT - shade))
    put:   level = min(FLOOR, max(low_water  + cushion, SHORT + shade))

It only ever moves in the protective direction, and it is capped a `shade`
inside the short strike because past the short strike a debit spread has
nothing more to earn -- intrinsic is pinned at the width, and a trigger above
the short strike would only sell a full-width spread into its own drag.
Without --cushion the level is the fixed FLOOR, which is what this did before.
"""
import argparse
import logging
import sys
import time

from trading_engine import orphans as o
from trading_engine.data_feed import fetch_spot

ap = argparse.ArgumentParser()
ap.add_argument("symbol")
ap.add_argument("long_strike", type=float)
ap.add_argument("short_strike", type=float)
ap.add_argument("floor", type=float,
                help="the level the trigger can never be looser than")
ap.add_argument("--cushion", type=float, default=0.0,
                help="trail this many points behind the best print (0 = fixed)")
ap.add_argument("--shade", type=float, default=0.50,
                help="cap the trailed level this far inside the short strike")
ap.add_argument("--poll", type=float, default=15.0)
a = ap.parse_args()

sym, lo, hi = a.symbol.upper(), a.long_strike, a.short_strike
is_call = lo < hi
logging.basicConfig(filename=f"/app/underlying_trigger_{sym}.log", level=logging.INFO,
                    format="%(asctime)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)

level = a.floor
best = None          # high-water (call) or low-water (put) print since arming
logging.info("ARMED: close %s %g/%g (%s) if %s %s %.2f%s", sym, lo, hi,
             "call" if is_call else "put", sym, "<" if is_call else ">", level,
             f", trailing {a.cushion:g} behind the best print, capped {a.shade:g} inside {hi:g}"
             if a.cushion > 0 else " (fixed)")


def _trail(px: float) -> float:
    """The level after this print. Moves only in the protective direction."""
    global best
    if a.cushion <= 0:
        return level
    if is_call:
        best = px if best is None else max(best, px)
        return max(level, min(best - a.cushion, hi - a.shade))
    best = px if best is None else min(best, px)
    return min(level, max(best + a.cushion, hi + a.shade))


while True:
    try:
        px = fetch_spot(sym)
        if px is None:
            logging.warning("%s: no spot -- retrying", sym)
            time.sleep(a.poll)
            continue
        new = _trail(px)
        if new != level:
            logging.info("%s %.2f: level %.2f -> %.2f (best %.2f)", sym, px, level, new, best)
            level = new
        else:
            logging.info("%s %.2f  (level %.2f)", sym, px, level)
        crossed = px < level if is_call else px > level
        if not crossed:
            time.sleep(a.poll)
            continue
        sts = [s for s in (o.open_structures() or [])
               if s["root"] == sym and s["long_strike"] == lo and s["short_strike"] == hi]
        if not sts:
            logging.info("TRIGGERED at %.2f but %s %g/%g not found (already closed?) -- exiting",
                         px, sym, lo, hi)
            break
        st = sts[0]
        m = o._mark(st)
        if not m:
            logging.warning("TRIGGERED at %.2f but no mark -- retrying", px)
            time.sleep(5)
            continue
        res = o._close(st, "OPERATOR_UNDERLYING_TRIGGER", m[0])
        e = abs(st["entry"])
        logging.info("TRIGGERED at %.2f (level %.2f): closed %s %g/%g x%d at %.2f "
                     "(entry %.2f, books %+.0f) -> %s",
                     px, level, sym, lo, hi, st["qty"], m[0], e,
                     (m[0] - e) * st["qty"] * 100, res)
        break
    except Exception:
        logging.exception("trigger loop error")
        time.sleep(a.poll)
