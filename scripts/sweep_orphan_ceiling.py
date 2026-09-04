"""Sweep the orphan CEILING against the engine's own logged series.

Same expiry-valued counterfactual as the other two (section 81). The ceiling
fires when the MARK reaches a fraction of the structure's maximum return --
so on an in-the-money spread it cannot fire until the short leg's time premium
has decayed, which is late in the session by construction.
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sweep_giveback import load, pnl, expiry_value


def replay_ceiling(rows, frac):
    entry = rows[0]["entry"]
    credit = rows[0]["credit"]
    width = rows[0]["width"]
    if entry <= 0:
        return None, None
    max_ret = (width - entry) / entry * 100.0 if not credit else (entry / entry) * 100.0
    if not credit:
        target = frac * max_ret
    else:
        target = frac * 100.0
    for idx, r in enumerate(rows):
        ret = ((entry - r["value"]) if credit else (r["value"] - entry)) / entry * 100.0
        if ret >= target:
            return r["value"], idx
    return None, None


def main():
    series = {k: v for k, v in load(sys.argv[1]).items() if len(v) >= 10}
    print(f"{len(series)} structure-days\n")
    print("  %-10s %6s %6s %10s %10s %10s" % (
        "ceiling", "fires", "helps", "rule", "hold", "difference"))
    for frac in (0.50, 0.60, 0.70, 0.75, 0.80, 0.90, 1.00):
        fires = helps = 0
        rt = ht = 0.0
        for (day, root, right, lo, hi, _e), rows in series.items():
            last = rows[-1]
            ev = expiry_value(root, day, right, lo, hi, last["credit"])
            if ev is None:
                continue
            hold = pnl(last["entry"], ev, last["qty"], last["credit"])
            val, idx = replay_ceiling(rows, frac)
            if val is None:
                rule = hold
            else:
                fires += 1
                rule = pnl(rows[idx]["entry"], val, rows[idx]["qty"], rows[idx]["credit"])
                if rule > hold:
                    helps += 1
            rt += rule
            ht += hold
        print("  %-10s %6d %6d %+10.0f %+10.0f %+10.0f" % (
            f"{frac:.2f}", fires, helps, rt, ht, rt - ht))


if __name__ == "__main__":
    main()
