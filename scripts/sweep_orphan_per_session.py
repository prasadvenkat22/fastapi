"""Every candidate config, split by session -- the both-halves test.

Two sessions is not a sample. Splitting them is the only check available on
whether a setting is a parameter or an artefact of one afternoon.
"""
import sys, os
from collections import defaultdict
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sweep_giveback import load, pnl, expiry_value
from sweep_fasttimer import combo

series = {k: v for k, v in load(sys.argv[1]).items() if len(v) >= 6}
days = sorted({k[0] for k in series})

hold = defaultdict(float)
for (day, root, right, lo, hi, _e), rs in series.items():
    ev = expiry_value(root, day, right, lo, hi, rs[-1]["credit"])
    if ev is not None:
        hold[day] += pnl(rs[-1]["entry"], ev, rs[-1]["qty"], rs[-1]["credit"])

print("  hold to expiry: " + "   ".join(f"{d} {hold[d]:+.0f}" for d in days)
      + f"   ALL {sum(hold.values()):+.0f}\n")
hdr = "  %-34s" % "config"
for d in days:
    hdr += " %12s" % str(d)[5:]
hdr += " %12s %8s" % ("ALL", "helps")
print(hdr)

CONFIGS = [
    ("old: -25% touch, no slow", -25, 0, None, 0),
    ("-40% touch, no slow", -40, 0, None, 0),
    ("-40% / 2min + -20%/30min", -40, 2, -20, 30),
    ("-40% / 5min + -20%/30min", -40, 5, -20, 30),
    ("-40% / 10min + -20%/30min  (LIVE)", -40, 10, -20, 30),
    ("-40% / 2min only", -40, 2, None, 0),
    ("-20%/30min only", None, 0, -20, 30),
    ("no stop at all", None, 0, None, 0),
]
for label, fast, fmin, slow, smin in CONFIGS:
    tot = defaultdict(float)
    helps = 0
    for (day, root, right, lo, hi, _e), rs in series.items():
        ev = expiry_value(root, day, right, lo, hi, rs[-1]["credit"])
        if ev is None:
            continue
        hd = pnl(rs[-1]["entry"], ev, rs[-1]["qty"], rs[-1]["credit"])
        v, i, why = combo(rs, fast, fmin, slow, smin)
        if v is None:
            tot[day] += hd
        else:
            got = pnl(rs[i]["entry"], v, rs[i]["qty"], rs[i]["credit"])
            if got > hd:
                helps += 1
            tot[day] += got
    line = "  %-34s" % label
    for d in days:
        line += " %+12.0f" % (tot[d] - hold[d])
    line += " %+12.0f %8d" % (sum(tot.values()) - sum(hold.values()), helps)
    print(line)
