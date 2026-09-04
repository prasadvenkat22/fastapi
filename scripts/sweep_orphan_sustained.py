"""Sustained-condition exits: a threshold that must HOLD, with a reset.

Two rules, both using a duration counter that resets the moment the condition
clears -- the shape in the user's sample code:

  DOWNSIDE  mark at or below -X% of entry, continuously for N minutes.
            Distinct from every stop measured so far, which fire on the first
            bar through the level. A 60-minute version cannot be tripped by a
            morning dip that recovers.

  UPSIDE    profit at or above Y% of MAXIMUM (not of peak), and then no new
            high for M minutes. This is the ceiling plus a stall confirmation:
            the ceiling alone has no duration test, so it books the instant
            the level prints.

Hold is valued at true expiry against the underlying's close.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sweep_giveback import load, pnl, expiry_value


def sustained_stop(rows, pct, minutes):
    ent, credit = rows[0]["entry"], rows[0]["credit"]
    since = None
    for idx, r in enumerate(rows):
        ret = ((ent - r["value"]) if credit else (r["value"] - ent)) / ent * 100.0
        if ret <= pct:
            if since is None:
                since = r["ts"]
            if (r["ts"] - since).total_seconds() / 60.0 >= minutes:
                return r["value"], idx
        else:
            since = None                 # reset the moment it recovers
    return None, None


def ceiling_stall(rows, frac, minutes):
    ent, credit = rows[0]["entry"], rows[0]["credit"]
    width = rows[0]["width"]
    max_profit = (width - ent) if not credit else ent
    if max_profit <= 0:
        return None, None
    target = max_profit * frac
    peak, peak_at = None, None
    for idx, r in enumerate(rows):
        prof = ((ent - r["value"]) if credit else (r["value"] - ent))
        if peak is None or prof > peak:
            peak, peak_at = prof, r["ts"]
            continue
        if prof < target:
            continue
        if (r["ts"] - peak_at).total_seconds() / 60.0 >= minutes:
            return r["value"], idx
    return None, None


def score(series, fn, *a):
    tot = 0.0
    f = h = 0
    for (day, root, right, lo, hi, _e), rs in series.items():
        ev = expiry_value(root, day, right, lo, hi, rs[-1]["credit"])
        if ev is None:
            continue
        hd = pnl(rs[-1]["entry"], ev, rs[-1]["qty"], rs[-1]["credit"])
        v, i = fn(rs, *a)
        if v is None:
            tot += hd
        else:
            f += 1
            got = pnl(rs[i]["entry"], v, rs[i]["qty"], rs[i]["credit"])
            if got > hd:
                h += 1
            tot += got
    return tot, f, h


def main():
    series = {k: v for k, v in load(sys.argv[1]).items() if len(v) >= 10}
    hold, _, _ = score(series, lambda rs: (None, None))
    print(f"  hold to expiry = {hold:+.0f}   ({len(series)} structure-days)\n")
    print("  DOWNSIDE -- mark below the level, sustained, resets on recovery")
    print("  %-8s %-9s %6s %6s %10s %11s" % ("level", "sustained", "fires", "helps", "P&L", "vs hold"))
    for pct in (-10, -20, -25, -40):
        for mins in (0, 30, 60):
            t, f, h = score(series, sustained_stop, pct, mins)
            print("  %-8s %-9s %6d %6d %+10.0f %+11.0f" % (
                f"{pct}%", f"{mins} min", f, h, t, t - hold))
    print("\n  UPSIDE -- at N% of MAX profit, then no new high for M minutes")
    print("  %-8s %-9s %6s %6s %10s %11s" % ("of max", "stalled", "fires", "helps", "P&L", "vs hold"))
    for frac in (0.60, 0.75, 0.80, 0.90, 0.95):
        for mins in (0, 15, 30):
            t, f, h = score(series, ceiling_stall, frac, mins)
            print("  %-8s %-9s %6d %6d %+10.0f %+11.0f" % (
                f"{frac:.0%}", f"{mins} min", f, h, t, t - hold))


if __name__ == "__main__":
    main()
