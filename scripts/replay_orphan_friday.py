"""Friday's manual book under the EXACT ladder deployed now.

    fast stop  -40% of entry, confirmed 2 min, suppressed while intrinsic > entry
    slow stop  -20% of entry, held 30 min continuously, resets on recovery,
               NOT suppressed by intrinsic
    ceiling    entry + 0.80 x (width - entry)
    stall      3.3 pts of intrinsic return, 5 min quiet, must book a gain
    flatten    15:45          nothing acts before 10:00
    weeklies   90% of width and nothing else
"""
import sys, os
from datetime import time as dtime, date, timedelta
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sweep_giveback import load, pnl, expiry_value

DAY = date(2026, 9, 4)
NY = timedelta(hours=-4)


def ny(ts):
    return (ts + NY).time()


def ladder(rows, expires_today, old=False):
    ent, credit, width = rows[0]["entry"], rows[0]["credit"], rows[0]["width"]
    if width <= 0 or ent <= 0:
        return None, None, None
    if not expires_today:                       # weeklies: one rule
        for i, r in enumerate(rows):
            hit = (r["value"] <= width * 0.10) if credit else (r["value"] >= width * 0.90)
            if hit:
                return r["value"], i, "LATER_TARGET"
        return None, None, None
    ceiling = ent + (0.90 if old else 0.80) * (width - ent)
    STOP_PCT = -25 if old else -40
    CONFIRM = 0 if old else 2
    SLOW = None if old else -20
    peak_iv = peak_at = None
    stop_since = slow_since = None
    for i, r in enumerate(rows):
        t = ny(r["ts"])
        past_hold = t >= dtime(10, 0)
        iv = r["intr"]
        ret = ((ent - r["value"]) if credit else (r["value"] - ent)) / ent * 100.0
        if peak_iv is None or (iv < peak_iv if credit else iv > peak_iv):
            peak_iv, peak_at = iv, r["ts"]
        pays = (iv < ent) if credit else (iv > ent)
        # fast stop: -40%, 2 minutes, intrinsic-guarded
        if past_hold and ret <= STOP_PCT and not pays:
            if stop_since is None:
                stop_since = r["ts"]
            if (r["ts"] - stop_since).total_seconds() / 60.0 >= CONFIRM:
                return r["value"], i, "STOP"
        else:
            stop_since = None
        if t >= dtime(15, 45):
            return r["value"], i, "FLATTEN"
        if ret >= (ceiling - ent) / ent * 100.0:
            return r["value"], i, "CEILING"
        # slow stop: -20% held 30 min, no intrinsic guard
        if past_hold and SLOW is not None and ret <= SLOW:
            if slow_since is None:
                slow_since = r["ts"]
            if (r["ts"] - slow_since).total_seconds() / 60.0 >= 30:
                return r["value"], i, "SLOW_STOP"
        else:
            slow_since = None
        # stall
        if past_hold and peak_at is not None:
            quiet = (r["ts"] - peak_at).total_seconds() / 60.0
            spct = ((ent - iv) if credit else (iv - ent)) / ent * 100.0
            ppct = ((ent - peak_iv) if credit else (peak_iv - ent)) / ent * 100.0
            gain = ((ent - r["value"]) if credit else (r["value"] - ent)) > 0
            if ppct > 0 and quiet >= 5 and spct <= ppct - 3.3 and gain:
                return r["value"], i, "STALL"
    return None, None, None


series = {k: v for k, v in load(sys.argv[1]).items() if len(v) >= 6 and k[0] == DAY}
print()
print("  ladders compared on the SAME set, so the double-counted structures")
print("  and the mis-valued weeklies cancel. Only the DIFFERENCE is meaningful.")
print()
print("  %-22s %3s %-11s %-11s %10s %10s %9s" % (
    "structure", "qty", "OLD exit", "NEW exit", "OLD", "NEW", "delta"))
tn = te = 0.0
for (day, root, right, lo, hi, _e), rs in sorted(series.items()):
    ev = expiry_value(root, day, right, lo, hi, rs[-1]["credit"])
    if ev is None:
        continue
    expires_today = True          # every logged series on this day is 0DTE
    hold = pnl(rs[-1]["entry"], ev, rs[-1]["qty"], rs[-1]["credit"])
    out = []
    for old in (True, False):
        v, i, why = ladder(rs, expires_today, old)
        got = hold if v is None else pnl(rs[i]["entry"], v, rs[i]["qty"], rs[i]["credit"])
        out.append((got, why or "expiry"))
    te += out[0][0]
    tn += out[1][0]
    d = out[1][0] - out[0][0]
    if abs(d) >= 1:
        print("  %-22s %3d %-11s %-11s %+10.0f %+10.0f %+9.0f" % (
            f"{root} {lo}/{hi}{right}", rs[-1]["qty"], out[0][1], out[1][1],
            out[0][0], out[1][0], d))
print()
print()
print("  %-22s %3s %-11s %-11s %+10.0f %+10.0f %+9.0f" % (
    "TOTAL (both ladders)", "", "", "", te, tn, tn - te))
print()
print()
print("  Friday actually realised  -2,464.00   equity closed 12,615.20")
print("  the rule change is worth  %+.0f" % (tn - te))
print("  so under today's rules    %+.2f   equity ~%.2f" % (-2464.00 + (tn-te), 12615.20 + (tn-te)))
