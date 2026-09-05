"""A profit target as a RETURN ON COST, and a hybrid that switches on moneyness.

The fraction-of-max ceiling is unreachable on a deep ITM spread because most
of the width is already paid for -- 8 paid on a 10-wide leaves 2 of upside,
and the short leg's time premium eats it until expiry. But a MODEST return on
cost, 8 -> 9, may well be reachable while 90% of max is not.

    RETURN   sell at entry x (1 + r). Same target whatever the geometry.
    HYBRID   deep spreads (entry >= X% of width) use the return target;
             the rest keep the fraction-of-max ceiling, which they can
             actually reach.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sweep_giveback import load, pnl, expiry_value


def fire(rows, mode, r_pct, frac, deep_at):
    ent, credit, width = rows[0]["entry"], rows[0]["credit"], rows[0]["width"]
    if width <= 0 or ent <= 0:
        return None, None
    deep = (ent / width) >= deep_at
    maxret = (width - ent) / ent * 100.0
    for idx, r in enumerate(rows):
        ret = ((ent - r["value"]) if credit else (r["value"] - ent)) / ent * 100.0
        if mode == "return":
            hit = ret >= r_pct
        elif mode == "maxfrac":
            hit = ret >= frac * maxret
        else:                                   # hybrid
            hit = ret >= (r_pct if deep else frac * maxret)
        if hit:
            return r["value"], idx
    return None, None


series = {k: v for k, v in load(sys.argv[1]).items() if len(v) >= 6}
hold = 0.0
for (day, root, right, lo, hi, _e), rs in series.items():
    ev = expiry_value(root, day, right, lo, hi, rs[-1]["credit"])
    if ev is not None:
        hold += pnl(rs[-1]["entry"], ev, rs[-1]["qty"], rs[-1]["credit"])
print(f"  hold to expiry = {hold:+.0f}   ({len(series)} structure-days)\n")


def run(label, mode, r_pct, frac, deep_at):
    tot = 0.0
    f = h = 0
    for (day, root, right, lo, hi, _e), rs in series.items():
        ev = expiry_value(root, day, right, lo, hi, rs[-1]["credit"])
        if ev is None:
            return
        hd = pnl(rs[-1]["entry"], ev, rs[-1]["qty"], rs[-1]["credit"])
        v, i = fire(rs, mode, r_pct, frac, deep_at)
        if v is None:
            tot += hd
        else:
            f += 1
            got = pnl(rs[i]["entry"], v, rs[i]["qty"], rs[i]["credit"])
            if got > hd:
                h += 1
            tot += got
    print("  %-32s %6d %6d %+10.0f %+11.0f" % (label, f, h, tot, tot - hold))


print("  %-32s %6s %6s %10s %11s" % ("rule", "fires", "helps", "P&L", "vs hold"))
for r in (5, 10, 15, 20, 30, 50):
    run(f"sell at +{r}% on cost", "return", r, 0, 0)
print()
for deep in (0.60, 0.70, 0.80):
    for r in (10, 20, 30):
        run(f"hybrid: deep>={deep:.0%} +{r}%, else 90% max", "hybrid", r, 0.90, deep)
