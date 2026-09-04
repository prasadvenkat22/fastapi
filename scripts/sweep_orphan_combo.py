"""Fast stop for a collapse, slow sustained stop for a grind. Do both beat one?

    FAST   -X% on the first bar through it -- a real breakdown should not wait
    SLOW   -Y% held continuously for N minutes -- a grind that never recovers

The two answer different questions, so running both is not redundant: the fast
level catches a gap, the slow one catches erosion the fast level never reaches.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sweep_giveback import load, pnl, expiry_value


def combo(rows, fast, slow, mins, guard=False):
    ent, credit = rows[0]["entry"], rows[0]["credit"]
    since = None
    for idx, r in enumerate(rows):
        ret = ((ent - r["value"]) if credit else (r["value"] - ent)) / ent * 100.0
        # STOP_RESPECTS_INTRINSIC: stand down while the position still pays at
        # expiry. The fast stop has always had this; whether the slow one
        # should is the question here.
        pays = (r["intr"] < ent) if credit else (r["intr"] > ent)
        if guard and pays:
            since = None
            continue
        if fast is not None and ret <= fast:
            return r["value"], idx, "FAST"
        if slow is not None and ret <= slow:
            if since is None:
                since = r["ts"]
            if (r["ts"] - since).total_seconds() / 60.0 >= mins:
                return r["value"], idx, "SLOW"
        else:
            since = None
    return None, None, None


series = {k: v for k, v in load(sys.argv[1]).items() if len(v) >= 10}
hold = 0.0
for (day, root, right, lo, hi, _e), rs in series.items():
    ev = expiry_value(root, day, right, lo, hi, rs[-1]["credit"])
    if ev is not None:
        hold += pnl(rs[-1]["entry"], ev, rs[-1]["qty"], rs[-1]["credit"])
print(f"  hold to expiry = {hold:+.0f}\n")
print("  %-10s %-18s %-8s %6s %6s %10s %11s   %s" % (
    "fast", "slow", "intrinsic", "fires", "helps", "P&L", "vs hold", "mix"))
for fast, slow, mins, guard in ((-40, -20, 30, False), (-40, -20, 30, True),
                                (-40, -10, 60, False), (-40, -10, 60, True),
                                (-40, -15, 45, False), (-40, -15, 45, True),
                                (-40, None, 0, True)):
    tot = 0.0
    f = h = 0
    mix = {}
    for (day, root, right, lo, hi, _e), rs in series.items():
        ev = expiry_value(root, day, right, lo, hi, rs[-1]["credit"])
        if ev is None:
            continue
        hd = pnl(rs[-1]["entry"], ev, rs[-1]["qty"], rs[-1]["credit"])
        v, i, why = combo(rs, fast, slow, mins, guard)
        if v is None:
            tot += hd
        else:
            f += 1
            mix[why] = mix.get(why, 0) + 1
            got = pnl(rs[i]["entry"], v, rs[i]["qty"], rs[i]["credit"])
            if got > hd:
                h += 1
            tot += got
    print("  %-10s %-18s %-8s %6d %6d %+10.0f %+11.0f   %s" % (
        f"{fast}%" if fast else "none",
        f"{slow}% / {mins}min" if slow else "none",
        "guarded" if guard else "no guard",
        f, h, tot, tot - hold, mix))
