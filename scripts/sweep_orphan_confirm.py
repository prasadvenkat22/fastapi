"""How long should the fast stop wait before firing? Current ladder, stall off."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sweep_giveback import load, pnl, expiry_value
from ablation import ladder

series = {k: v for k, v in load(sys.argv[1]).items() if len(v) >= 6}
hold = 0.0
for (day, root, right, lo, hi, _e), rs in series.items():
    ev = expiry_value(root, day, right, lo, hi, rs[-1]["credit"])
    if ev is not None:
        hold += pnl(rs[-1]["entry"], ev, rs[-1]["qty"], rs[-1]["credit"])

print()
print("  fast stop -40%%, stall OFF, slow stop -20%%/30min, flatten 15:45")
print("  hold everything to expiry = %+.0f\n" % hold)
print("  %-12s %6s %6s %10s %11s   %s" % (
    "wait", "fires", "helps", "total", "vs 0 min", "exits"))
base = None
for c in (0, 1, 2, 3, 5, 10, 20):
    tot = 0.0
    f = h = 0
    mix = {}
    for (day, root, right, lo, hi, _e), rs in series.items():
        ev = expiry_value(root, day, right, lo, hi, rs[-1]["credit"])
        if ev is None:
            continue
        hd = pnl(rs[-1]["entry"], ev, rs[-1]["qty"], rs[-1]["credit"])
        v, i, why = ladder(rs, confirm=c, stall_on=False)
        if v is None:
            tot += hd
        else:
            got = pnl(rs[i]["entry"], v, rs[i]["qty"], rs[i]["credit"])
            if why == "STOP":
                f += 1
                if got > hd:
                    h += 1
            mix[why] = mix.get(why, 0) + 1
            tot += got
    if base is None:
        base = tot
    print("  %-12s %6d %6d %+10.0f %+11.0f   %s" % (
        f"{c} min", f, h, tot, tot - base, mix))
