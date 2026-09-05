"""A ceiling that reads INTRINSIC, so it can actually fire on a deep ITM spread.

The deployed ceiling reads the MARK and therefore needs the short leg's time
premium to have decayed -- which does not happen until expiry is close, so it
fires almost never (section 85, 24 structures, zero fires at 0.80 and above).

The obvious fix is to measure the same fraction against INTRINSIC: a spread
whose intrinsic is 90% of its width IS at 90% of maximum, whatever the mark
says. It fires constantly on an ITM book.

The catch, and the reason this needs measuring rather than assuming: it still
SELLS at the mark. Firing on intrinsic while transacting at a mark held below
entry by time premium books a loss on a position at maximum payoff.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sweep_giveback import load, pnl, expiry_value


def ceil_intr(rows, frac, basis):
    ent, credit, width = rows[0]["entry"], rows[0]["credit"], rows[0]["width"]
    if width <= 0:
        return None, None
    for idx, r in enumerate(rows):
        if basis == "intrinsic":
            reached = (r["intr"] >= width * frac) if not credit else (r["intr"] <= width * (1 - frac))
        else:
            maxret = (width - ent) / ent * 100.0
            ret = ((ent - r["value"]) if credit else (r["value"] - ent)) / ent * 100.0
            reached = ret >= frac * maxret
        if reached:
            return r["value"], idx
    return None, None


series = {k: v for k, v in load(sys.argv[1]).items() if len(v) >= 6}
hold = 0.0
for (day, root, right, lo, hi, _e), rs in series.items():
    ev = expiry_value(root, day, right, lo, hi, rs[-1]["credit"])
    if ev is not None:
        hold += pnl(rs[-1]["entry"], ev, rs[-1]["qty"], rs[-1]["credit"])
print(f"  hold to expiry = {hold:+.0f}   ({len(series)} structure-days)\n")
print("  %-9s %-11s %6s %6s %10s %11s" % ("of max", "measured on", "fires", "helps", "P&L", "vs hold"))
for basis in ("intrinsic", "mark"):
    for frac in (0.70, 0.80, 0.90, 0.95, 1.00):
        tot = 0.0
        f = h = 0
        for (day, root, right, lo, hi, _e), rs in series.items():
            ev = expiry_value(root, day, right, lo, hi, rs[-1]["credit"])
            if ev is None:
                continue
            hd = pnl(rs[-1]["entry"], ev, rs[-1]["qty"], rs[-1]["credit"])
            v, i = ceil_intr(rs, frac, basis)
            if v is None:
                tot += hd
            else:
                f += 1
                got = pnl(rs[i]["entry"], v, rs[i]["qty"], rs[i]["credit"])
                if got > hd:
                    h += 1
                tot += got
        print("  %-9s %-11s %6d %6d %+10.0f %+11.0f" % (
            f"{frac:.0%}", basis, f, h, tot, tot - hold))
    print()
