"""What today booked, against what the deployed ladder would have produced."""
import sys, os
from datetime import time as dtime
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sweep_giveback import load, pnl, expiry_value
from replay_day import run_ladder

OLD = dict(stop_pct=-25, ceiling_frac=0.90, stall_pts=3.3, stall_min=5,
           must_book_gain=True, flatten_at=dtime(15, 45), hold_until=dtime(10, 0))
NEW = dict(stop_pct=-40, ceiling_frac=0.75, stall_pts=3.3, stall_min=5,
           must_book_gain=True, flatten_at=dtime(15, 45), hold_until=dtime(10, 0))

series = {k: v for k, v in load(sys.argv[1]).items() if len(v) >= 10}
print("  %-24s %3s %9s %-9s %9s %-9s %10s" % (
    "structure", "qty", "OLD", "why", "NEW", "why", "difference"))
to = tn = 0.0
rows = []
for (day, root, right, lo, hi, _e), rs in series.items():
    ev = expiry_value(root, day, right, lo, hi, rs[-1]["credit"])
    if ev is None:
        continue
    hold = pnl(rs[-1]["entry"], ev, rs[-1]["qty"], rs[-1]["credit"])
    out = []
    for cfg in (OLD, NEW):
        v, i, why = run_ladder(rs, **cfg)
        out.append((hold, "expiry") if v is None else
                   (pnl(rs[i]["entry"], v, rs[i]["qty"], rs[i]["credit"]), why))
    to += out[0][0]
    tn += out[1][0]
    d = out[1][0] - out[0][0]
    if abs(d) >= 1:
        rows.append((d, root, lo, hi, right, rs[-1]["qty"], out))
for d, root, lo, hi, right, qty, out in sorted(rows, reverse=True):
    print("  %-24s %3d %+9.0f %-9s %+9.0f %-9s %+10.0f" % (
        f"{root} {lo}/{hi}{right}", qty,
        out[0][0], out[0][1], out[1][0], out[1][1], d))
print("\n  %-24s %3s %+9.0f %-9s %+9.0f %-9s %+10.0f" % (
    "TOTAL (39 structures)", "", to, "", tn, "", tn - to))
