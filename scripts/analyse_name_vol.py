"""Realised movement per name, to size spread width against.

A spread's width only means something relative to how far the underlying
actually travels in a day. A 3-wide QQQ spread is a different trade from a
3-wide SNDK spread, and the book has been treating them alike.
"""
import warnings
warnings.filterwarnings("ignore")
import yfinance as yf
import statistics

NAMES = ["QQQ", "SNDK", "MU", "NVDA", "PANW", "DELL", "STX", "WDC", "AVGO", "CRWV", "MRVL"]
print()
print("  %-6s %9s %9s %9s %9s %9s" % (
    "name", "price", "avg range", "as % px", "avg |move|", "as % px"))
out = {}
for n in NAMES:
    try:
        h = yf.Ticker(n).history(period="30d", interval="1d")
        if len(h) < 10:
            print("  %-6s  insufficient history" % n)
            continue
        px = float(h["Close"].iloc[-1])
        rng = [float(r["High"] - r["Low"]) for _, r in h.iterrows()]
        mv = [abs(float(h["Close"].iloc[i] - h["Close"].iloc[i - 1]))
              for i in range(1, len(h))]
        ar, am = statistics.mean(rng), statistics.mean(mv)
        out[n] = (px, ar, am)
        print("  %-6s %9.2f %9.2f %8.1f%% %9.2f %8.1f%%" % (
            n, px, ar, ar / px * 100, am, am / px * 100))
    except Exception as e:
        print("  %-6s  failed: %s" % (n, e))

print()
print("  SUGGESTED WIDTH -- about 2x the average daily range, so the short")
print("  strike is a real distance away rather than a coin flip")
print()
print("  %-6s %9s %11s %13s %13s" % ("name", "price", "avg range", "width ~2x rng", "as % of price"))
for n, (px, ar, am) in out.items():
    w = 2 * ar
    print("  %-6s %9.2f %11.2f %13.1f %12.1f%%" % (n, px, ar, w, w / px * 100))
