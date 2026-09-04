"""Per-structure detail at the deployed setting, so the total can be checked."""
import sys
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
from sweep_giveback import load, replay, pnl, expiry_value

PCT, CONFIRM = float(sys.argv[2]), float(sys.argv[3])
series = {k: v for k, v in load(sys.argv[1]).items() if len(v) >= 10}
print(f"  giveback {PCT:.0f}% of width, confirm {CONFIRM:.0f} min\n")
print("  %-22s %8s %10s %10s %10s" % ("structure", "fired", "rule", "hold@expiry", "diff"))
tot_r = tot_h = 0.0
for (day, root, right, lo, hi, _e), rows in sorted(series.items()):
    last = rows[-1]
    ev = expiry_value(root, day, right, lo, hi, last["credit"])
    if ev is None:
        continue
    hold = pnl(last["entry"], ev, last["qty"], last["credit"])
    val, idx = replay(rows, PCT, CONFIRM)
    rule = hold if val is None else pnl(rows[idx]["entry"], val, rows[idx]["qty"], rows[idx]["credit"])
    tot_r += rule
    tot_h += hold
    if val is not None:
        print("  %-22s %8s %+10.0f %+10.0f %+10.0f" % (
            f"{day} {root} {lo}/{hi}{right}",
            rows[idx]["ts"].strftime("%H:%M"), rule, hold, rule - hold))
print("\n  %-22s %8s %+10.0f %+10.0f %+10.0f" % ("TOTAL (all structures)", "", tot_r, tot_h, tot_r - tot_h))
