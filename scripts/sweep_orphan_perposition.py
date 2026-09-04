"""Per-position: what each stop setting would have produced, against expiry."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sweep_giveback import load, pnl, expiry_value
from sweep_stop import replay_stop

series = {k: v for k, v in load(sys.argv[1]).items() if len(v) >= 10}
STOPS = [-25, -40, -60, None]
print("  %-26s %3s %8s %8s %8s %8s %10s" % (
    "structure", "qty", "-25%", "-40%", "-60%", "no stop", "best gain"))
rows = []
for (day, root, right, lo, hi, _e), rs in sorted(series.items()):
    last = rs[-1]
    ev = expiry_value(root, day, right, lo, hi, last["credit"])
    if ev is None:
        continue
    hold = pnl(last["entry"], ev, last["qty"], last["credit"])
    out = []
    for st in STOPS:
        if st is None:
            out.append(hold)
            continue
        val, idx = replay_stop(rs, st, True)
        out.append(hold if val is None else
                   pnl(rs[idx]["entry"], val, rs[idx]["qty"], rs[idx]["credit"]))
    gain = max(out) - out[0]
    rows.append((gain, root, lo, hi, right, last["qty"], out, day))
rows.sort(reverse=True)
tot = [0.0] * 4
for gain, root, lo, hi, right, qty, out, day in rows:
    for i, v in enumerate(out):
        tot[i] += v
    if abs(gain) >= 1:
        print("  %-26s %3d %8.0f %8.0f %8.0f %8.0f %+10.0f" % (
            f"{root} {lo}/{hi}{right}", qty, out[0], out[1], out[2], out[3], gain))
print("\n  %-26s %3s %8.0f %8.0f %8.0f %8.0f %+10.0f" % (
    "TOTAL (all 39)", "", tot[0], tot[1], tot[2], tot[3], max(tot) - tot[0]))
