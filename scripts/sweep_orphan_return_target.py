"""A target expressed as RETURN ON COST -- sell when the mark reaches
entry x (1 + r). Distinct from the ceiling, which is a fraction of MAX profit
and therefore capped by the width."""
import sys, os
from datetime import time as dtime, timedelta
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sweep_giveback import load, pnl, expiry_value

NY = timedelta(hours=-4)


def ny(ts):
    return (ts + NY).time()


def ladder(rows, target_ret=None):
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
        if t >= dtime(15, 45):
            return r["value"], i, "FLATTEN"
        if target_ret is not None and ret >= target_ret:
            return r["value"], i, "TARGET"
        if past and ret <= -20:
            slow_since = slow_since or r["ts"]
            if (r["ts"] - slow_since).total_seconds() / 60.0 >= 30:
                return r["value"], i, "SLOW"
        else:
            slow_since = None
    return None, None, None


series = {k: v for k, v in load(sys.argv[1]).items() if len(v) >= 6}
print()
print("  %-22s %6s %6s %11s %11s   %s" % (
    "target (return on cost)", "fires", "helps", "total", "vs off", "detail"))
base = None
for tr in (None, 30, 40, 50, 75, 100):
    tot = 0.0
    f = h = 0
    who = []
    for (day, root, right, lo, hi, _e), rs in series.items():
        ev = expiry_value(root, day, right, lo, hi, rs[-1]["credit"])
        if ev is None:
            continue
        hd = pnl(rs[-1]["entry"], ev, rs[-1]["qty"], rs[-1]["credit"])
        v, i, why = ladder(rs, tr)
        got = hd if v is None else pnl(rs[i]["entry"], v, rs[i]["qty"], rs[i]["credit"])
        if why == "TARGET":
            f += 1
            if got > hd:
                h += 1
            who.append("%s %s/%s %+.0f vs %+.0f" % (root, lo, hi, got, hd))
        tot += got
    if base is None:
        base = tot
    print("  %-22s %6d %6d %+11.0f %+11.0f   %s" % (
        "off" if tr is None else f"+{tr}%", f, h, tot, tot - base, "; ".join(who)))
