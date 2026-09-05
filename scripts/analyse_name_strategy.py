"""Per-name structure guidance from realised movement.

Directional debit spreads want the underlying to TRAVEL: they pay when it
moves past the short strike. Condors want it to SIT. So the same statistic --
how far the name actually goes in a day -- points opposite ways for the two,
and it is measurable.

30 daily bars. Thin, and real.
"""
import warnings, statistics
warnings.filterwarnings("ignore")
import yfinance as yf

NAMES = ["QQQ", "NVDA", "AVGO", "PANW", "MRVL", "DELL", "WDC", "STX", "MU", "SNDK", "CRWV"]
rows = []
for n in NAMES:
    try:
        h = yf.Ticker(n).history(period="30d", interval="1d")
        if len(h) < 10:
            continue
        px = float(h["Close"].iloc[-1])
        rng = statistics.mean(float(r["High"] - r["Low"]) for _, r in h.iterrows())
        mv = [abs(float(h["Close"].iloc[i] - h["Close"].iloc[i-1])) for i in range(1, len(h))]
        amv = statistics.mean(mv)
        # how often does it sit still? -- the condor question
        quiet = sum(1 for m in mv if m < 0.5 * rng) / len(mv)
        rows.append((n, px, rng, amv, rng / px * 100, quiet))
    except Exception:
        pass

rows.sort(key=lambda r: r[4])
print()
print("  %-6s %8s %8s %7s %9s %9s   %s" % (
    "name", "price", "day rng", "% px", "width 2x", "sits still", "structure that fits"))
for n, px, rng, amv, pct, quiet in rows:
    if pct < 3.0:
        s = "condor / credit -- it sits"
    elif pct < 5.5:
        s = "either; debit if trending"
    else:
        s = "DEBIT only -- too wild to sell"
    print("  %-6s %8.2f %8.1f %6.1f%% %9.0f %8.0f%%   %s" % (
        n, px, rng, pct, 2 * rng, quiet * 100, s))

print()
print("  READING IT")
print("  'sits still' = share of days the close-to-close move stayed inside")
print("  HALF the average range. High means an iron condor has room; low")
print("  means the short strikes get run over.")
print()
print("  'width 2x' is a starting width -- twice the average daily range, so")
print("  the short strike is a genuine distance rather than a coin flip the")
print("  stock has usually already decided by mid-morning.")
