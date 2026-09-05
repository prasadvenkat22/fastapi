"""Diff the harness's indicator reconstruction against what the engine logged.

Both run the SAME agents (macd/sma/bollinger/rsi) over 5-minute QQQ bars. The
engine fetched period="5d" every minute and live; the harness downloads 60d
once and slices 390 bars. If the reconstruction is faithful the readings match
bar for bar. Where they diverge, so does every entry decision built on them --
and every entry parameter in this repo was tuned on the harness.
"""
import os
import sys
from datetime import date, time as dtime

os.environ.setdefault("SWEEP_CHAIN_PRICING", "1")
HERE = os.path.dirname(os.path.abspath(__file__))
for line in open(os.path.join(HERE, "trading.env"), encoding="utf-8"):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ[k] = v.strip().strip('"')
sys.path.insert(0, "C:/fastapi")

import scripts.sweep as S

DAY = date(2026, 9, 3)
S._patch_engine()
sessions = S._load_sessions("60d")
key = [k for k in sessions if k == DAY]
if not key:
    print(f"  no bars for {DAY} in the 60d download")
    raise SystemExit
bars = sessions[DAY]
print()
print(f"  harness reconstruction, {DAY}, {len(bars)} bars")
print("  the four terms clean_bull actually needs, plus sentiment (hardcoded GOOD)")
print("  %-7s %8s %-11s %-19s %-13s %-11s %s" % (
    "ET", "close", "sma_trend", "ema_cross", "vwap_side", "rsi_band", "clean_bull?"))
for i in range(len(bars)):
    ts = bars.index[i]
    if not (dtime(10, 30) <= ts.time() <= dtime(11, 10)):
        continue
    S._Clock.now = ts.to_pydatetime()
    seen = S._seen_at(ts)
    try:
        st = S._session_state(seen)
    except Exception as exc:
        print("  %-7s  state failed: %s" % (ts.strftime("%H:%M"), exc))
        continue
    ok = (st.get("sma_trend") == "ABOVE_SMA"
          and st.get("ema_cross") == "EMA9_ABOVE_SMA20"
          and st.get("vwap_side") == "ABOVE_VWAP"
          and st.get("rsi_band") == "BULL_BAND")
    print("  %-7s %8.2f %-11s %-19s %-13s %-11s %s" % (
        ts.strftime("%H:%M"), float(seen["Close"].iloc[-1]),
        st.get("sma_trend"), st.get("ema_cross"), st.get("vwap_side"),
        st.get("rsi_band"), "YES" if ok else "no"))
print()
print("  LIVE at 10:50 ET entered CLEAN with:")
print("     trend=ABOVE_SMA  ema9=EMA9_ABOVE_SMA20  vwap=ABOVE_VWAP  macro=GOOD")
