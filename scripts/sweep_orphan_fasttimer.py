"""Does the FAST stop need a confirmation timer too?

The fast stop fires on the first cycle through its level. One bad print, or a
single wide quote, is enough. A short confirmation would filter that -- the
question is what it costs in the cases where the collapse is real.

Both stops run together throughout, each with its own clock, each resetting
the moment the mark recovers above its own level.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sweep_giveback import load, pnl, expiry_value


def combo(rows, fast, fast_min, slow, slow_min):
    ent, credit = rows[0]["entry"], rows[0]["credit"]
    f_since = s_since = None
    for idx, r in enumerate(rows):
        ret = ((ent - r["value"]) if credit else (r["value"] - ent)) / ent * 100.0
        if fast is not None and ret <= fast:
            # the fast stop keeps its intrinsic guard
            pays = (r["intr"] < ent) if credit else (r["intr"] > ent)
            if not pays:
                if f_since is None:
                    f_since = r["ts"]
                if (r["ts"] - f_since).total_seconds() / 60.0 >= fast_min:
                    return r["value"], idx, "FAST"
            else:
                f_since = None
        else:
            f_since = None
        if slow is not None and ret <= slow:
            if s_since is None:
                s_since = r["ts"]
            if (r["ts"] - s_since).total_seconds() / 60.0 >= slow_min:
                return r["value"], idx, "SLOW"
        else:
            s_since = None
    return None, None, None


series = {k: v for k, v in load(sys.argv[1]).items() if len(v) >= 6}
hold = 0.0
for (day, root, right, lo, hi, _e), rs in series.items():
    ev = expiry_value(root, day, right, lo, hi, rs[-1]["credit"])
    if ev is not None:
        hold += pnl(rs[-1]["entry"], ev, rs[-1]["qty"], rs[-1]["credit"])
print(f"  hold to expiry = {hold:+.0f}   ({len(series)} structure-days)\n")
print("  %-16s %-16s %6s %6s %10s %11s   %s" % (
    "fast", "slow", "fires", "helps", "P&L", "vs hold", "mix"))
for fmin in (0, 2, 5, 10, 15, 30):
    tot = 0.0
    f = h = 0
    mix = {}
    for (day, root, right, lo, hi, _e), rs in series.items():
        ev = expiry_value(root, day, right, lo, hi, rs[-1]["credit"])
        if ev is None:
            continue
        hd = pnl(rs[-1]["entry"], ev, rs[-1]["qty"], rs[-1]["credit"])
        v, i, why = combo(rs, -40, fmin, -20, 30)
        if v is None:
            tot += hd
        else:
            f += 1
            mix[why] = mix.get(why, 0) + 1
            got = pnl(rs[i]["entry"], v, rs[i]["qty"], rs[i]["credit"])
            if got > hd:
                h += 1
            tot += got
    print("  %-16s %-16s %6d %6d %+10.0f %+11.0f   %s" % (
        f"-40% / {fmin}min", "-20% / 30min", f, h, tot, tot - hold, mix))
