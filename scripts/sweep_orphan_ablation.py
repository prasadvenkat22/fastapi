"""Ablation: what does each rule contribute, measured by switching it off.

Same set of structures every time, so the double-counted and mis-valued rows
cancel between arms. Only the DIFFERENCES are meaningful.
"""
import sys, os
from datetime import time as dtime, date, timedelta
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sweep_giveback import load, pnl, expiry_value

NY = timedelta(hours=-4)


def ny(ts):
    return (ts + NY).time()


def ladder(rows, stop_pct=-40, confirm=2, slow=-20, slow_min=30,
           ceil_frac=0.80, stall_on=True, flatten=True):
    ent, credit, width = rows[0]["entry"], rows[0]["credit"], rows[0]["width"]
    if width <= 0 or ent <= 0:
        return None, None, None
    ceiling_ret = ((ent + ceil_frac * (width - ent)) - ent) / ent * 100.0 if ceil_frac else None
    peak_iv = peak_at = stop_since = slow_since = None
    for i, r in enumerate(rows):
        t = ny(r["ts"])
        past = t >= dtime(10, 0)
        iv = r["intr"]
        ret = ((ent - r["value"]) if credit else (r["value"] - ent)) / ent * 100.0
        if peak_iv is None or (iv < peak_iv if credit else iv > peak_iv):
            peak_iv, peak_at = iv, r["ts"]
        pays = (iv < ent) if credit else (iv > ent)
        if stop_pct is not None and past and ret <= stop_pct and not pays:
            stop_since = stop_since or r["ts"]
            if (r["ts"] - stop_since).total_seconds() / 60.0 >= confirm:
                return r["value"], i, "STOP"
        else:
            stop_since = None
        if flatten and t >= dtime(15, 45):
            return r["value"], i, "FLATTEN"
        if ceiling_ret is not None and ret >= ceiling_ret:
            return r["value"], i, "CEILING"
        if slow is not None and past and ret <= slow:
            slow_since = slow_since or r["ts"]
            if (r["ts"] - slow_since).total_seconds() / 60.0 >= slow_min:
                return r["value"], i, "SLOW"
        else:
            slow_since = None
        if stall_on and past and peak_at is not None:
            quiet = (r["ts"] - peak_at).total_seconds() / 60.0
            spct = ((ent - iv) if credit else (iv - ent)) / ent * 100.0
            ppct = ((ent - peak_iv) if credit else (peak_iv - ent)) / ent * 100.0
            gain = ((ent - r["value"]) if credit else (r["value"] - ent)) > 0
            if ppct > 0 and quiet >= 5 and spct <= ppct - 3.3 and gain:
                return r["value"], i, "STALL"
    return None, None, None


series = {k: v for k, v in load(sys.argv[1]).items() if len(v) >= 6}
days = sorted({k[0] for k in series})


def score(**kw):
    tot = 0.0
    fires = {}
    for (day, root, right, lo, hi, _e), rs in series.items():
        ev = expiry_value(root, day, right, lo, hi, rs[-1]["credit"])
        if ev is None:
            continue
        hold = pnl(rs[-1]["entry"], ev, rs[-1]["qty"], rs[-1]["credit"])
        v, i, why = ladder(rs, **kw)
        tot += hold if v is None else pnl(rs[i]["entry"], v, rs[i]["qty"], rs[i]["credit"])
        if why:
            fires[why] = fires.get(why, 0) + 1
    return tot, fires


base, basefires = score()
print()
print(f"  {len(series)} structures across {len(days)} sessions ({days[0]} and {days[-1]})")
print()
print("  %-34s %11s %11s   %s" % ("configuration", "total", "vs full", "exits"))
print("  %-34s %+11.0f %11s   %s" % ("FULL LADDER (deployed)", base, "-", basefires))
for label, kw in (
    ("no fast stop", dict(stop_pct=None)),
    ("fast stop at -25%, no confirm", dict(stop_pct=-25, confirm=0)),
    ("no confirmation (fires on a touch)", dict(confirm=0)),
    ("no slow stop", dict(slow=None)),
    ("no stall", dict(stall_on=False)),
    ("no ceiling", dict(ceil_frac=0.0)),
    ("flatten only", dict(stop_pct=None, slow=None, stall_on=False, ceil_frac=0.0)),
    ("stops + flatten, NO stall/ceiling", dict(stall_on=False, ceil_frac=0.0)),
    ("  same, confirm 0", dict(stall_on=False, ceil_frac=0.0, confirm=0)),
    ("  same, slow 30min -> 45min", dict(stall_on=False, ceil_frac=0.0, slow_min=45)),
    ("  same, keep ceiling 0.80", dict(stall_on=False)),
):
    t, f = score(**kw)
    print("  %-34s %+11.0f %+11.0f   %s" % (label, t, t - base, f))
