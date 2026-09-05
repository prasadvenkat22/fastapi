"""A profit target as a fraction of the WIDTH, which is a lower bar than a
fraction of the max PROFIT and is what "pay 8, sell at 9" actually means.

    MU 995/1005, entry 6.72, width 10.00
      90% of max PROFIT = 6.72 + 0.90 x 3.28 = 9.67
      90% of WIDTH      =        0.90 x 10.00 = 9.00

Same label, 67 cents apart, and the second is reachable sooner. Worth
measuring separately rather than assuming the first stands in for it.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sweep_giveback import load, pnl, expiry_value


def fire(rows, frac):
    ent, credit, width = rows[0]["entry"], rows[0]["credit"], rows[0]["width"]
    if width <= 0:
        return None, None
    target = width * frac
    for idx, r in enumerate(rows):
        # a credit structure profits as the cost to close falls
        hit = (r["value"] <= width * (1 - frac)) if credit else (r["value"] >= target)
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
print("  %-22s %6s %6s %10s %11s" % ("target", "fires", "helps", "P&L", "vs hold"))
for frac in (0.60, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95):
    tot = 0.0
    f = h = 0
    for (day, root, right, lo, hi, _e), rs in series.items():
        ev = expiry_value(root, day, right, lo, hi, rs[-1]["credit"])
        if ev is None:
            continue
        hd = pnl(rs[-1]["entry"], ev, rs[-1]["qty"], rs[-1]["credit"])
        v, i = fire(rs, frac)
        if v is None:
            tot += hd
        else:
            f += 1
            got = pnl(rs[i]["entry"], v, rs[i]["qty"], rs[i]["credit"])
            if got > hd:
                h += 1
            tot += got
    print("  %-22s %6d %6d %+10.0f %+11.0f" % (
        f"{frac:.0%} of width", f, h, tot, tot - hold))
