"""When should the flatten run? It is the only profit-taking rule that works,
so its TIME is the one profit parameter worth tuning."""
import sys, os
from datetime import time as dtime, timedelta
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sweep_giveback import load, pnl, expiry_value

NY = timedelta(hours=-4)


def ny(ts):
    return (ts + NY).time()


def ladder(rows, flat_at):
    ent, credit, width = rows[0]["entry"], rows[0]["credit"], rows[0]["width"]
    if width <= 0 or ent <= 0:
        return None, None, None
    slow_since = None
    for i, r in enumerate(rows):
        t = ny(r["ts"])
        past = t >= dtime(10, 0)
        iv = r["intr"]
        ret = ((ent - r["value"]) if credit else (r["value"] - ent)) / ent * 100.0
        pays = (iv < ent) if credit else (iv > ent)
        if past and ret <= -40 and not pays:
            return r["value"], i, "STOP"
        if t >= flat_at:
            return r["value"], i, "FLATTEN"
        if past and ret <= -20:
            slow_since = slow_since or r["ts"]
            if (r["ts"] - slow_since).total_seconds() / 60.0 >= 30:
                return r["value"], i, "SLOW"
        else:
            slow_since = None
    return None, None, None


series = {k: v for k, v in load(sys.argv[1]).items() if len(v) >= 6}
print()
print("  %-14s %6s %11s %11s   %s" % ("flatten at", "fires", "total", "vs 15:45", "exits"))
base = None
for hh, mm in ((14, 30), (15, 0), (15, 15), (15, 30), (15, 45)):
    tot = 0.0
    mix = {}
    for (day, root, right, lo, hi, _e), rs in series.items():
        ev = expiry_value(root, day, right, lo, hi, rs[-1]["credit"])
        if ev is None:
            continue
        hd = pnl(rs[-1]["entry"], ev, rs[-1]["qty"], rs[-1]["credit"])
        v, i, why = ladder(rs, dtime(hh, mm))
        tot += hd if v is None else pnl(rs[i]["entry"], v, rs[i]["qty"], rs[i]["credit"])
        if why:
            mix[why] = mix.get(why, 0) + 1
    if (hh, mm) == (15, 45):
        base = tot
    print("  %-14s %6d %+11.0f %11s   %s" % (
        f"{hh:02d}:{mm:02d}", mix.get("FLATTEN", 0), tot, "", mix))
print()
print("  (the 15:45 row is the deployed setting; compare the others to it)")
