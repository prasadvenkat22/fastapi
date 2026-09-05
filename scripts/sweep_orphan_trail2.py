"""Trailing profit stop WITH a quiet timer -- peak must be N minutes old.

The naive version sells on the first wobble after any upward tick, because
the peak is set instantly and 5% of a tiny peak is pennies. Requiring the
peak to have STOOD for N minutes is what makes it a stall rather than a
twitch: a position still making new highs has not peaked yet.

This is structurally the engine's stall. What differs is the giveback
measure -- a percentage of peak PROFIT here, against the stall's fixed 3.3
points of return -- and whether it must book a gain.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sweep_giveback import load, pnl, expiry_value


def run(rows, pct, quiet_min, basis, must_gain):
    ent = rows[0]["entry"]
    credit = rows[0]["credit"]
    peak = None
    peak_at = None
    for idx, r in enumerate(rows):
        v = r["value"] if basis == "mark" else r["intr"]
        prof = ((ent - v) if credit else (v - ent))
        if peak is None or prof > peak:
            peak, peak_at = prof, r["ts"]
            continue                      # a new high is not a giveback
        if peak <= 0:
            continue
        quiet = (r["ts"] - peak_at).total_seconds() / 60.0
        if quiet < quiet_min:
            continue
        if prof > peak * (1.0 - pct / 100.0):
            continue
        if must_gain:
            realisable = ((ent - r["value"]) if credit else (r["value"] - ent))
            if realisable <= 0:
                continue
        return r["value"], idx
    return None, None


def main():
    series = {k: v for k, v in load(sys.argv[1]).items() if len(v) >= 10}
    hold = 0.0
    for (day, root, right, lo, hi, _e), rs in series.items():
        ev = expiry_value(root, day, right, lo, hi, rs[-1]["credit"])
        if ev is not None:
            hold += pnl(rs[-1]["entry"], ev, rs[-1]["qty"], rs[-1]["credit"])
    print(f"  hold to expiry = {hold:+.0f}   (39 structure-days)\n")
    print("  %-7s %-7s %-9s %-6s %6s %6s %10s %11s" % (
        "give%", "quiet", "basis", "gain?", "fires", "helps", "P&L", "vs hold"))
    for basis in ("intrinsic", "mark"):
        for must_gain in (True,):
            for pct in (5, 10, 20):
                for q in (0, 10, 20, 30, 45):
                    tot = 0.0
                    f = h = 0
                    for (day, root, right, lo, hi, _e), rs in series.items():
                        ev = expiry_value(root, day, right, lo, hi, rs[-1]["credit"])
                        if ev is None:
                            continue
                        hd = pnl(rs[-1]["entry"], ev, rs[-1]["qty"], rs[-1]["credit"])
                        v, i = run(rs, pct, q, basis, must_gain)
                        if v is None:
                            tot += hd
                        else:
                            f += 1
                            got = pnl(rs[i]["entry"], v, rs[i]["qty"], rs[i]["credit"])
                            if got > hd:
                                h += 1
                            tot += got
                    print("  %-7s %-7s %-9s %-6s %6d %6d %+10.0f %+11.0f" % (
                        f"{pct}%", f"{q} min", basis, "yes", f, h, tot, tot - hold))
            print()


if __name__ == "__main__":
    main()
