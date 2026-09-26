"""Where in the week's range, and which side of the midline, an entry would open. Section 241.

Operator, 2026-09-26: "never buy a call spread at the week high, never buy a put
spread at the week low", plus an outside note's multi-timeframe triggers (0DTE:
enter on a pullback below the 5-minute Bollinger midline; weekly: on an hourly
close below the 20-SMA midline).

MEASURED FIRST (scratch/bp/weekrange.py, out_weekrange.txt): 11 tradeable names,
52 sessions 07-07..09-17, 3,432 hourly decision points, direction only.
  at the week high (top 10% of the 5-session range): up 4 days later 34% of the
      time (median -1.99%) vs 53% mid-range; same day 42% vs 44%. Both halves.
  at the week low (bottom 10%): up 4 days later 62%, same day 54%. Both halves.
  hourly close below its 20-SMA: up 4 days later 60% vs 42% above.
  5-min price below its 20-SMA: up by the close 47% vs 43% above -- weak.
So the range guard and the hourly trigger are supported on direction; the
5-minute trigger barely. No premiums, one regime, overlapping points.

The note is written for bullish entries only; the bearish side is mirrored here
(a put waits for price ABOVE the midline), which is what the same measurement
implies. Every gate FAILS CLOSED: unreadable bars refuse the entry rather than
guess. Each is its own switch in /desk/settings, off in code.
"""

from __future__ import annotations

import logging
import os
import time
from collections import OrderedDict
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import httpx

from . import tradier_orders

logger = logging.getLogger(__name__)
NY = ZoneInfo("America/New_York")


def _on(key: str) -> bool:
    return os.getenv(key, "false").lower() == "true"


def _num(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, "") or default)
    except ValueError:
        return default


def weekrange_on() -> bool:
    return _on("TRADING_WEEKRANGE_GUARD")


def pullback_on(timeframe: str) -> bool:
    return _on("TRADING_PULLBACK_GATE_WEEKLY" if timeframe == "hourly" else "TRADING_PULLBACK_GATE_0DTE")


def macro_bad_puts_only() -> bool:
    return _on("TRADING_MACRO_BAD_PUTS_ONLY")


# ---- pure: the context from bars ------------------------------------------

def _sma(values: list, n: int = 20) -> Optional[float]:
    return sum(values[-n:]) / n if len(values) >= n else None


def context_from_bars(bars15: list, bars5: list, sessions: int = 5) -> Optional[dict]:
    """Week range, hourly and 5-minute midlines from 15-min and 5-min bars (both several days back).

    Bars are dicts with time ('YYYY-MM-DDTHH:MM:SS'), high, low, close, oldest first.
    """
    if not bars15:
        return None
    days = sorted({b["time"][:10] for b in bars15})[-sessions:]
    wk = [b for b in bars15 if b["time"][:10] in days]
    hi, lo = max(b["high"] for b in wk), min(b["low"] for b in wk)
    spot = (bars5[-1]["close"] if bars5 else bars15[-1]["close"])
    # Hourly closes: the last 15-min close in each clock hour of the session
    # (09:30-10:29 is the first hour), across the days fetched.
    hourly: "OrderedDict[tuple, float]" = OrderedDict()
    for b in bars15:
        hh, mm = int(b["time"][11:13]), int(b["time"][14:16])
        bucket = (b["time"][:10], (hh * 60 + mm - 570) // 60)
        hourly[bucket] = b["close"]
    hcloses = list(hourly.values())
    sma1h = _sma(hcloses)
    sma5 = _sma([b["close"] for b in bars5])
    return {
        "spot": spot, "week_high": hi, "week_low": lo, "sessions": len(days),
        "weekpos": (spot - lo) / (hi - lo) if hi > lo else None,
        "hourly_close": hcloses[-1] if hcloses else None, "sma1h": sma1h,
        "below_1h": (hcloses[-1] < sma1h) if sma1h is not None else None,
        "sma5": sma5, "below_5m": (spot < sma5) if sma5 is not None else None,
    }


def weekrange_refusal(bullish: bool, ctx: Optional[dict]) -> Optional[str]:
    """Why a call (bullish) at the week high or a put (bearish) at the week low is refused, or None."""
    if ctx is None or ctx.get("weekpos") is None:
        return "week range unreadable -- refusing rather than guessing"
    pos = ctx["weekpos"]
    if bullish and pos >= _num("TRADING_WEEKRANGE_CALL_MAX", 0.90):
        return (f"bullish at {pos:.0%} of the week's range ({ctx['week_low']:.2f}-{ctx['week_high']:.2f}), "
                f"at or above the {_num('TRADING_WEEKRANGE_CALL_MAX', 0.90):.0%} ceiling")
    if (not bullish) and pos <= _num("TRADING_WEEKRANGE_PUT_MIN", 0.10):
        return (f"bearish at {pos:.0%} of the week's range ({ctx['week_low']:.2f}-{ctx['week_high']:.2f}), "
                f"at or below the {_num('TRADING_WEEKRANGE_PUT_MIN', 0.10):.0%} floor")
    return None


def pullback_refusal(bullish: bool, ctx: Optional[dict], timeframe: str) -> Optional[str]:
    """The note's trigger: a call needs price BELOW the midline, a put ABOVE it (mirrored)."""
    key, label = ("below_1h", "hourly close vs its 20-SMA") if timeframe == "hourly" else ("below_5m", "5-min price vs its 20-SMA")
    below = None if ctx is None else ctx.get(key)
    if below is None:
        return f"{label} unreadable -- refusing rather than guessing"
    if bullish and not below:
        return f"bullish but {label} is ABOVE the midline -- waiting for a pullback"
    if (not bullish) and below:
        return f"bearish but {label} is BELOW the midline -- waiting for a bounce"
    return None


# ---- the fetch --------------------------------------------------------------

_CACHE: dict = {}
_TTL_S = 60.0


def _timesales(symbol: str, interval: str, start: str, end: str) -> list:
    r = httpx.get(f"{tradier_orders._base()}/markets/timesales",
                  params={"symbol": symbol, "interval": interval, "start": start, "end": end,
                          "session_filter": "open"},
                  headers=tradier_orders._headers(), timeout=10.0)
    r.raise_for_status()
    data = ((r.json() or {}).get("series") or {}).get("data") or []
    if isinstance(data, dict):
        data = [data]
    out = []
    for b in data:
        try:
            out.append({"time": str(b["time"]), "high": float(b["high"]),
                        "low": float(b["low"]), "close": float(b["close"])})
        except (KeyError, TypeError, ValueError):
            continue
    return out


def week_context(symbol: str) -> Optional[dict]:
    """Live context for one symbol, cached 60 s; None if the bars cannot be read."""
    hit = _CACHE.get(symbol)
    if hit and time.time() - hit[0] < _TTL_S:
        return hit[1]
    ctx = None
    try:
        now = datetime.now(NY)
        start = (now - timedelta(days=9)).strftime("%Y-%m-%d 09:30")
        end = now.strftime("%Y-%m-%d 16:00")
        # 5-min from four calendar days back, so the 20-bar midline exists at
        # the open (20 bars of today alone would take until ~11:10 ET, and the
        # gate fails closed without it).
        start5 = (now - timedelta(days=4)).strftime("%Y-%m-%d 09:30")
        ctx = context_from_bars(_timesales(symbol, "15min", start, end),
                                _timesales(symbol, "5min", start5, end))
    except Exception:
        logger.warning("Week context unreadable for %s.", symbol, exc_info=True)
    _CACHE[symbol] = (time.time(), ctx)
    return ctx
