"""Replay today's QQQ positions against different exit settings.

WHAT IS REAL: the marks. Every line comes from the engine's own ORPHAN log --
the value it actually saw, at the time it actually saw it, priced at what it
could have transacted at. No reconstruction, no model.

WHAT IS NOT: only the positions the engine marked today, and only until they
closed. A rule that would have held LONGER than the real exit has no data past
that point, so those runs are truncated at the last observed mark and marked
TRUNCATED. That biases against patient rules, and the bias is named rather
than hidden.

SIX POSITIONS, which is not a sample. This answers "were today's settings the
best available today", not "are these the right settings".
"""
import re
import sys
from collections import defaultdict
from datetime import datetime

LOG = "/var/log/qqq-trading.log"
RX = re.compile(
    r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ INFO ORPHAN QQQ P "
    r"(\d+)/(\d+) x(\d+) debit: entry ([\d.]+) value ([\d.]+) ([-+][\d.]+)%"
)

series = defaultdict(list)
for line in open(LOG, errors="replace"):
    m = RX.match(line)
    if not m:
        continue
    ts, lo, hi, qty, entry, val, ret = m.groups()
    key = (f"{lo}/{hi}", float(entry))
    series[key].append((datetime.strptime(ts, "%Y-%m-%d %H:%M:%S"),
                        float(val), float(ret), int(qty)))

for k in series:
    series[k].sort()


def run(marks, entry, target, stop, confirm_min, stall_min, giveback_pct):
    """Walk the marks once, first rule to fire wins. Returns (why, ret, qty)."""
    peak = None
    peak_at = None
    stop_since = None
    for t, val, ret, qty in marks:
        if peak is None or ret > peak:
            peak, peak_at = ret, t
        # TARGET
        if ret >= target:
            return "TARGET", ret, qty
        # STALL: peak quiet for stall_min, and given back giveback_pct OF THE PEAK
        if peak is not None and peak > 0:
            quiet = (t - peak_at).total_seconds() / 60.0
            if quiet >= stall_min and ret <= peak * (1 - giveback_pct / 100.0):
                return "STALL", ret, qty
        # STOP with confirmation
        if ret <= stop:
            if stop_since is None:
                stop_since = t
            elif (t - stop_since).total_seconds() / 60.0 >= confirm_min:
                return "STOP", ret, qty
        else:
            stop_since = None
    return "TRUNCATED", marks[-1][2], marks[-1][3]


CONFIGS = [
    ("this morning   tgt30 stop-10 cf0 stall5/20", 30, -10, 0, 5, 20),
    ("NOW            tgt30 stop-35 cf5 stall2/20", 30, -35, 5, 2, 20),
    ("               tgt30 stop-35 cf5 stall2/10", 30, -35, 5, 2, 10),
    ("               tgt30 stop-35 cf5 stall2/30", 30, -35, 5, 2, 30),
    ("               tgt30 stop-35 cf5 stall5/20", 30, -35, 5, 5, 20),
    ("               tgt40 stop-35 cf5 stall2/20", 40, -35, 5, 2, 20),
    ("               tgt25 stop-35 cf5 stall2/20", 25, -35, 5, 2, 20),
    ("               tgt30 stop-20 cf5 stall2/20", 30, -20, 5, 2, 20),
    ("               no exits, ride to last mark", 999, -999, 0, 999, 999),
]

print(f"{len(series)} QQQ position(s) with logged marks today\n")
for k, marks in sorted(series.items()):
    print(f"  {k[0]:<10} entry {k[1]:.2f}  {len(marks)} marks  "
          f"peak {max(m[2] for m in marks):+.1f}%  "
          f"worst {min(m[2] for m in marks):+.1f}%")

print(f"\n{'config':<46}{'total $':>10}  outcomes")
print("-" * 92)
for name, tgt, stop, cf, sm, gb in CONFIGS:
    total = 0.0
    outs = []
    for (label, entry), marks in sorted(series.items()):
        why, ret, qty = run(marks, entry, tgt, stop, cf, sm, gb)
        pnl = entry * (ret / 100.0) * qty * 100
        total += pnl
        outs.append(f"{why[:4]}{ret:+.0f}")
    print(f"{name:<46}{total:>+10.0f}  {' '.join(outs)}")

print("\nTRUNCATED = the rule would have held past the real exit; no data "
      "beyond that point, so it is scored at the last mark seen.")
print("SIX positions. This says which settings were best TODAY, not which are "
      "right.")
