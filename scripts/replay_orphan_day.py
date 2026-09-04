"""Replay a full session's orphan book under a given exit ladder.

Runs the SAME ladder the engine runs -- stop with the intrinsic guard, ceiling,
stall with its quiet timer and book-a-gain test, flatten -- over the engine's
own one-minute logs, and values anything still open at expiry against the
underlying's real close (section 81).

Reported against two baselines: what the ladder produces, and what holding
every position to expiry would have produced.
"""
import sys
import os
from datetime import time as dtime, timedelta

# THE LOG IS UTC. The container runs UTC and every rule in the engine is
# expressed in New York. A first version of this compared a 15:45 flatten
# against UTC timestamps, so it fired at 11:45 ET on every position and
# reported FLATTEN 34 times out of 39 -- a result that looked like data.
NY_OFFSET = timedelta(hours=-4)   # EDT for the sessions in these logs


def ny(ts):
    return (ts + NY_OFFSET).time()

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sweep_giveback import load, pnl, expiry_value


def run_ladder(rows, stop_pct, ceiling_frac, stall_pts, stall_min,
               must_book_gain, flatten_at, hold_until):
    entry = rows[0]["entry"]
    credit = rows[0]["credit"]
    width = rows[0]["width"]
    max_ret = (width - entry) / entry * 100.0 if entry else 0.0
    ceiling = ceiling_frac * max_ret if ceiling_frac > 0 else None
    peak_iv = None
    peak_at = None
    for idx, r in enumerate(rows):
        t = ny(r["ts"])
        past_hold = (hold_until is None) or (t >= hold_until)
        i = r["intr"]
        ret = ((entry - r["value"]) if credit else (r["value"] - entry)) / entry * 100.0
        if peak_iv is None or (i < peak_iv if credit else i > peak_iv):
            peak_iv, peak_at = i, r["ts"]
        # stop, respecting intrinsic
        if past_hold and ret <= stop_pct:
            pays = (i < entry) if credit else (i > entry)
            if not pays:
                return r["value"], idx, "STOP"
        # flatten
        if flatten_at is not None and t >= flatten_at:
            return r["value"], idx, "FLATTEN"
        # ceiling
        if ceiling is not None and ret >= ceiling:
            return r["value"], idx, "CEILING"
        # stall
        if past_hold and stall_min > 0 and peak_at is not None:
            quiet = (r["ts"] - peak_at).total_seconds() / 60.0
            spct = ((entry - i) if credit else (i - entry)) / entry * 100.0
            ppct = ((entry - peak_iv) if credit else (peak_iv - entry)) / entry * 100.0
            gain = ((entry - r["value"]) if credit else (r["value"] - entry)) > 0
            if (ppct > 0 and quiet >= stall_min and spct <= ppct - stall_pts
                    and (gain or not must_book_gain)):
                return r["value"], idx, "STALL"
    return None, None, None


def main():
    series = {k: v for k, v in load(sys.argv[1]).items() if len(v) >= 10}
    configs = [
        ("TODAY as deployed", dict(stop_pct=-25, ceiling_frac=0.90, stall_pts=3.3,
                                   stall_min=5, must_book_gain=True,
                                   flatten_at=dtime(15, 45), hold_until=dtime(10, 0))),
        ("NEW (stop -40, ceil .75)", dict(stop_pct=-40, ceiling_frac=0.75, stall_pts=3.3,
                                          stall_min=5, must_book_gain=True,
                                          flatten_at=dtime(15, 45), hold_until=dtime(10, 0))),
        ("NEW, no ceiling", dict(stop_pct=-40, ceiling_frac=0.0, stall_pts=3.3,
                                 stall_min=5, must_book_gain=True,
                                 flatten_at=dtime(15, 45), hold_until=dtime(10, 0))),
        ("flatten only", dict(stop_pct=-999, ceiling_frac=0.0, stall_pts=0,
                              stall_min=0, must_book_gain=True,
                              flatten_at=dtime(15, 45), hold_until=dtime(10, 0))),
    ]
    print("  %-26s %10s %10s %8s %s" % ("config", "P&L", "vs hold", "exits", "mix"))
    hold_total = 0.0
    for (day, root, right, lo, hi, _e), rs in series.items():
        ev = expiry_value(root, day, right, lo, hi, rs[-1]["credit"])
        if ev is not None:
            hold_total += pnl(rs[-1]["entry"], ev, rs[-1]["qty"], rs[-1]["credit"])
    for label, cfg in configs:
        total = 0.0
        mix = {}
        n = 0
        for (day, root, right, lo, hi, _e), rs in series.items():
            ev = expiry_value(root, day, right, lo, hi, rs[-1]["credit"])
            if ev is None:
                continue
            hold = pnl(rs[-1]["entry"], ev, rs[-1]["qty"], rs[-1]["credit"])
            val, idx, why = run_ladder(rs, **cfg)
            if val is None:
                total += hold
            else:
                n += 1
                mix[why] = mix.get(why, 0) + 1
                total += pnl(rs[idx]["entry"], val, rs[idx]["qty"], rs[idx]["credit"])
        print("  %-26s %+10.0f %+10.0f %8d %s" % (label, total, total - hold_total, n, mix))
    print("\n  %-26s %+10.0f" % ("hold everything to expiry", hold_total))


if __name__ == "__main__":
    main()
