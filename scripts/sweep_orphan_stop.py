"""Sweep the orphan STOP against the engine's own logged series.

Same data and the same expiry-valued counterfactual as sweep_giveback: the
hold outcome is intrinsic against the underlying's real close, never the last
logged mark, because the exit itself ends the log (section 81).

Two dimensions:
  stop_pct        the mark-based trigger, as today
  respect_intr    whether the stop stands down while intrinsic still exceeds
                  entry -- STOP_RESPECTS_INTRINSIC, section 67

Reports worst single position too, since the question being asked is about
capping a loss rather than about the average.
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sweep_giveback import load, pnl, expiry_value


def replay_stop(rows, stop_pct, respect_intr):
    entry = rows[0]["entry"]
    credit = rows[0]["credit"]
    for idx, r in enumerate(rows):
        ret = ((entry - r["value"]) if credit else (r["value"] - entry)) / entry * 100.0
        if ret > stop_pct:
            continue
        if respect_intr:
            ok = (r["intr"] < entry) if credit else (r["intr"] > entry)
            if ok:
                continue          # still pays at expiry -- suppressed
        return r["value"], idx
    return None, None


def main():
    series = {k: v for k, v in load(sys.argv[1]).items() if len(v) >= 10}
    print(f"{len(series)} structure-days\n")
    print("  %-8s %-14s %6s %6s %10s %10s %10s %10s" % (
        "stop", "intrinsic", "fires", "helps", "rule", "hold", "difference", "worst"))
    for respect in (True, False):
        for stop in (-5, -10, -15, -25, -40, -60):
            fires = helps = 0
            rt = ht = 0.0
            worst = 0.0
            for (day, root, right, lo, hi, _e), rows in series.items():
                last = rows[-1]
                ev = expiry_value(root, day, right, lo, hi, last["credit"])
                if ev is None:
                    continue
                hold = pnl(last["entry"], ev, last["qty"], last["credit"])
                val, idx = replay_stop(rows, stop, respect)
                if val is None:
                    rule = hold
                else:
                    fires += 1
                    rule = pnl(rows[idx]["entry"], val, rows[idx]["qty"], rows[idx]["credit"])
                    if rule > hold:
                        helps += 1
                rt += rule
                ht += hold
                worst = min(worst, rule)
            print("  %-8s %-14s %6d %6d %+10.0f %+10.0f %+10.0f %+10.0f" % (
                f"{stop:.0f}%", "respected" if respect else "IGNORED",
                fires, helps, rt, ht, rt - ht, worst))
        print()


if __name__ == "__main__":
    main()
