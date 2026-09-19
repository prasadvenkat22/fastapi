"""Session flow on Tradier 5-minute bars: is the tape being bought or sold NOW?

Built 2026-09-19 for the single-name rotation. weekly_signals.intraday_accumulation
measures the same idea on yfinance HOURLY bars, which at 09:45 has fewer than
two bars and returns nothing until about 11:30 -- useless as an entry gate for
a rotation whose first run is 09:45. This reads Tradier's 5-minute bars through
the same client the order path uses, needs three bars (09:45), and is cached
per symbol for a minute.

THE GATE. A call debit is taken only when all four agree that buyers are
paying up:

    spot        above the running session VWAP
    slope       VWAP higher than it was SLOPE_BARS bars ago (30 min): the
                average price PAID is rising, not just the last print
    bars above  at least BARS_ABOVE_MIN of bars closed above the running VWAP
    close loc   the volume-weighted close location is positive: volume is
                arriving on closes near the highs of their bars

A put debit needs the mirror image. Anything short of all four is a refusal
in veto mode and a logged line in record mode. No bars, or fewer than
MIN_BARS, is a refusal too: an entry gate that cannot read the tape does not
wave the trade through.

WHAT IT IS NOT. Not a measure of institutional buying -- every buyer has a
seller, and nothing in a public OHLCV feed tells them apart. It measures
URGENCY, whether buyers paid up through the session or waited, and urgency is
what a large order worked against a VWAP benchmark leaves behind.

UNMEASURED. Turned on live at the operator's decision before any outcome data
existed. Every candidate's reading is logged as a FLOW line so the veto can be
scored against outcomes afterwards (section 194).
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime

import httpx

from . import tradier_orders

logger = logging.getLogger(__name__)

# veto: refuse a structure the flow contradicts. record: log what veto would
# have done, refuse nothing. off: do not read the tape at all.
MODE = os.getenv("TRADING_DTE0_VWAP_GATE", "veto").strip().lower()
MIN_BARS = int(os.getenv("TRADING_VWAP_MIN_BARS", "3"))
SLOPE_BARS = int(os.getenv("TRADING_VWAP_SLOPE_BARS", "6"))
BARS_ABOVE_MIN = float(os.getenv("TRADING_VWAP_BARS_ABOVE_MIN", "0.60"))

_CACHE: dict = {}
_TTL_S = 60.0


def bars_for(symbol: str, day: str) -> list:
    """Tradier 5-minute bars for the regular session, oldest first. Public so
    the replay can ask about a past day."""
    r = httpx.get(f"{tradier_orders._base()}/markets/timesales",
                  params={"symbol": symbol, "interval": "5min",
                          "start": f"{day} 09:30", "end": f"{day} 16:00",
                          "session_filter": "open"},
                  headers=tradier_orders._headers(), timeout=10.0)
    r.raise_for_status()
    data = ((r.json() or {}).get("series") or {}).get("data") or []
    if isinstance(data, dict):
        data = [data]
    out = []
    for b in data:
        try:
            out.append({"time": str(b.get("time") or ""),
                        "high": float(b["high"]), "low": float(b["low"]),
                        "close": float(b["close"]), "volume": float(b.get("volume") or 0),
                        "vwap": float(b.get("vwap") or b.get("price") or b["close"])})
        except (KeyError, TypeError, ValueError):
            continue
    return out


def flow_from_bars(bars: list, spot: "float | None" = None) -> "dict | None":
    """The four readings from a list of bars. Pure, so it can be tested."""
    if len(bars) < MIN_BARS:
        return None
    cum_pv = cum_v = 0.0
    running = []
    above = 0
    clv_v = 0.0
    for b in bars:
        v = b["volume"]
        cum_pv += b["vwap"] * v
        cum_v += v
        vwap = cum_pv / cum_v if cum_v > 0 else b["close"]
        running.append(vwap)
        if b["close"] > vwap:
            above += 1
        rng = b["high"] - b["low"]
        if rng > 0:
            clv_v += ((b["close"] - b["low"]) - (b["high"] - b["close"])) / rng * v
    vwap_now = running[-1]
    ref = running[-1 - SLOPE_BARS] if len(running) > SLOPE_BARS else running[0]
    slope_pct = (vwap_now / ref - 1.0) * 100.0 if ref else 0.0
    px = spot if spot is not None else bars[-1]["close"]
    return {
        "spot": round(px, 4),
        "vwap": round(vwap_now, 4),
        "above_vwap": px > vwap_now,
        "slope_pct": round(slope_pct, 4),
        "bars_above": round(above / len(bars), 4),
        "ad_ratio": round(clv_v / cum_v, 4) if cum_v > 0 else 0.0,
        "bars": len(bars),
    }


def session_flow(symbol: str) -> "dict | None":
    """Today's flow for one underlying, or None when the tape cannot be read."""
    symbol = symbol.upper()
    hit = _CACHE.get(symbol)
    if hit and time.time() - hit[0] < _TTL_S:
        return hit[1]
    out = None
    try:
        from zoneinfo import ZoneInfo
        day = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
        bars = bars_for(symbol, day)
        spot = None
        try:
            from .data_feed import fetch_spot
            spot = fetch_spot(symbol)
        except Exception:
            spot = None
        out = flow_from_bars(bars, spot)
    except Exception:
        logger.warning("FLOW %s: tape unreadable.", symbol, exc_info=True)
        out = None
    _CACHE[symbol] = (time.time(), out)
    return out


def gate(direction: str, flow: "dict | None") -> "tuple[bool, str]":
    """(allowed, why). direction is 'bullish' for call debits, 'bearish' for puts."""
    if not flow:
        return False, "no tape read (fewer than %d bars or the feed failed)" % MIN_BARS
    bull = direction == "bullish"
    fails = []
    if bull:
        if not flow["above_vwap"]:
            fails.append("spot %.2f under VWAP %.2f" % (flow["spot"], flow["vwap"]))
        if flow["slope_pct"] <= 0:
            fails.append("VWAP slope %+.3f%% not rising" % flow["slope_pct"])
        if flow["bars_above"] < BARS_ABOVE_MIN:
            fails.append("only %.0f%% of bars above VWAP" % (100 * flow["bars_above"]))
        if flow["ad_ratio"] <= 0:
            fails.append("volume closing low in its bars (%+.2f)" % flow["ad_ratio"])
    else:
        if flow["above_vwap"]:
            fails.append("spot %.2f over VWAP %.2f" % (flow["spot"], flow["vwap"]))
        if flow["slope_pct"] >= 0:
            fails.append("VWAP slope %+.3f%% not falling" % flow["slope_pct"])
        if (1.0 - flow["bars_above"]) < BARS_ABOVE_MIN:
            fails.append("only %.0f%% of bars below VWAP" % (100 * (1 - flow["bars_above"])))
        if flow["ad_ratio"] >= 0:
            fails.append("volume closing high in its bars (%+.2f)" % flow["ad_ratio"])
    if fails:
        return False, "; ".join(fails)
    return True, "buyers paying up" if bull else "sellers hitting bids"


def describe(flow: "dict | None") -> str:
    if not flow:
        return "FLOW n/a"
    return ("FLOW spot %.2f %s VWAP %.2f, slope %+.3f%%/%d bars, %.0f%% bars above, "
            "close-loc %+.2f, %d bars" % (
                flow["spot"], "above" if flow["above_vwap"] else "under", flow["vwap"],
                flow["slope_pct"], SLOPE_BARS, 100 * flow["bars_above"],
                flow["ad_ratio"], flow["bars"]))
