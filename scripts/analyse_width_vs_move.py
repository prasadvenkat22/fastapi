"""Widths traded, against the distance the underlying actually covers."""
import warnings, sys, os
warnings.filterwarnings("ignore")
import yfinance as yf, statistics
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sweep_giveback import load

d = load(sys.argv[1])
used = {}
for k, rs in d.items():
    root = k[1]
    w = rs[0]["width"]
    if w > 0:
        used.setdefault(root, set()).add(w)

rng = {}
for n in used:
    try:
        h = yf.Ticker(n).history(period="30d", interval="1d")
        if len(h) >= 10:
            rng[n] = statistics.mean(float(r["High"] - r["Low"]) for _, r in h.iterrows())
    except Exception:
        pass

print()
print("  %-6s %11s %14s %12s   %s" % (
    "name", "avg day rng", "widths traded", "widest/range", "verdict"))
for n in sorted(used):
    if n not in rng:
        continue
    ws = sorted(used[n])
    ratio = max(ws) / rng[n]
    v = ("sized to the move" if ratio >= 1.5 else
         "narrow" if ratio >= 0.8 else "FAR too narrow")
    print("  %-6s %11.1f %14s %11.2fx   %s" % (
        n, rng[n], ",".join("%g" % x for x in ws[:4]), ratio, v))

print()
print("  a width below the daily range makes the spread BINARY: the stock")
print("  routinely travels past both strikes, so by the close it is worth")
print("  either the full width or nothing. That is why intrinsic averaged")
print("  92-95%% of width AT ENTRY across this book -- the spreads were")
print("  already decided when they were bought.")
