"""The WEEK's VWAP as an entry gate for the weekly book.  Section 210.

Operator, 2026-09-21: "add vwap gated entries for call spreads and puts for
weeklies as well -- institutions may get in and out on particular stocks."

WHY NOT THE SESSION GATE. The weekly book already passes through vwap_gate
because it enters through the same rotation script, and section 209 measured
what that read is worth over the horizon a weekly actually holds: nothing.
468 symbol-days, the 09:50 tape, the move to the close four sessions later --
a put-ok morning rose 1.69% on average against 1.84% for a call-ok one. A
session VWAP resets every day; a position that spans five sessions needs the
level the WEEK's volume transacted at, which is what an institution working
an order against a benchmark leaves behind and what this module reads.

THE ANCHOR. Cumulative volume-weighted price from Monday's 09:30 to now, built
from Tradier's 5-minute bars session by session -- the precise form section
109 said was available once the daily-bar approximation in weekly_signals
earned its place. On a Monday morning it IS the session VWAP (there is no
earlier volume in the week); by Wednesday's 09:50 it carries two full
sessions and the anchor moves slowly, which is the point.

THE GATE, two conditions, direction-keyed:

    side     spot ABOVE the anchored VWAP for a call debit, BELOW for a put,
             with a band of TOL_ATR x ATR around the level in which the read
             is AT and neither side passes -- the same quarter-ATR tolerance
             weekly_signals uses, so a label does not flip on noise the
             position cannot feel
    slope    the anchored VWAP higher than SLOPE_BARS bars ago for a call,
             lower for a put: the price the week's volume is paying is still
             moving the structure's way, not just the last print

No read (no bars, the feed failed) is a refusal, as in vwap_gate: an entry
gate that cannot read the tape does not wave the trade through.

UNMEASURED, AND THE ONE HINT POINTS THE OTHER WAY. The only outcome data with
an anchored-side label is fifteen settled rows of the Friday credit shadow
(section 209): seven short call spreads on names BELOW their week's VWAP lost
62% on average because the names came back up through the strike. Seven rows
and a different structure, so it settles nothing -- but it is mean reversion,
not momentum, and this gate is a momentum rule. MODE therefore exists:
`record` logs what veto would do and refuses nothing. The operator chose veto
from day one, as with vwap_gate (section 194); every candidate's reading is
logged as a WEEKVWAP line so the choice can be scored either way.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

NY = ZoneInfo("America/New_York")

# veto: refuse a weekly structure the week's VWAP contradicts. record: log what
# veto would have done, refuse nothing. off: do not read it at all.
MODE = os.getenv("TRADING_WEEKLY_VWAP_GATE", "veto").strip().lower()
# The AT band, in ATRs of the underlying, on either side of the level.
TOL_ATR = float(os.getenv("TRADING_WEEKLY_VWAP_TOL_ATR", "0.25") or 0)
# Slope is the anchored VWAP now against this many 5-minute bars ago.
SLOPE_BARS = int(os.getenv("TRADING_WEEKLY_VWAP_SLOPE_BARS", "6") or 6)
MIN_BARS = int(os.getenv("TRADING_WEEKLY_VWAP_MIN_BARS", "3") or 3)

_CACHE: dict = {}
_TTL_S = 60.0


def _is_trading_day(d: date) -> bool:
    try:
        from .market_calendar import is_trading_day
        return bool(is_trading_day(d))
    except Exception:
        return d.weekday() < 5


def week_sessions(today: date) -> list:
    """Trading days from the Monday of `today`'s week through `today`, oldest first."""
    monday = today - timedelta(days=today.weekday())
    out = []
    d = monday
    while d <= today:
        if _is_trading_day(d):
            out.append(d)
        d += timedelta(days=1)
    return out


def anchored_bars(symbol: str, today: "date | None" = None) -> list:
    """Every regular-session 5-minute bar from Monday's open through now."""
    from .vwap_gate import bars_for
    today = today or datetime.now(NY).date()
    bars = []
    for d in week_sessions(today):
        try:
            bars.extend(bars_for(symbol, d.isoformat()))
        except Exception as exc:  # noqa: BLE001 - one missing session is a warning, not a crash
            logger.warning("WEEKVWAP %s: no bars for %s (%s).", symbol, d, exc)
    return bars


def flow_from_bars(bars: list, spot: "float | None" = None,
                   tol: float = 0.0) -> "dict | None":
    """The anchored read from a list of bars spanning the week. Pure, so it can
    be tested. `tol` is the AT band in price units (TOL_ATR x ATR)."""
    if len(bars) < MIN_BARS:
        return None
    cum_pv = cum_v = 0.0
    running = []
    above = 0
    for b in bars:
        v = float(b.get("volume") or 0.0)
        cum_pv += float(b["vwap"]) * v
        cum_v += v
        vwap = cum_pv / cum_v if cum_v > 0 else float(b["close"])
        running.append(vwap)
        if float(b["close"]) > vwap:
            above += 1
    vwap_now = running[-1]
    ref = running[-1 - SLOPE_BARS] if len(running) > SLOPE_BARS else running[0]
    slope_pct = (vwap_now / ref - 1.0) * 100.0 if ref else 0.0
    px = float(spot) if spot is not None else float(bars[-1]["close"])
    if px > vwap_now + tol:
        side = "ABOVE"
    elif px < vwap_now - tol:
        side = "BELOW"
    else:
        side = "AT"
    sessions = len({str(b.get("time", ""))[:10] for b in bars})
    return {
        "spot": round(px, 4),
        "vwap_week": round(vwap_now, 4),
        "side": side,
        "tol": round(tol, 4),
        "slope_pct": round(slope_pct, 4),
        "bars_above": round(above / len(bars), 4),
        "bars": len(bars),
        "sessions": sessions,
    }


def read(symbol: str, atr: "float | None" = None) -> "dict | None":
    """This week's anchored read for one underlying, or None when unreadable.
    Cached a minute per symbol, like the session gate."""
    symbol = symbol.upper()
    hit = _CACHE.get(symbol)
    if hit and time.time() - hit[0] < _TTL_S:
        return hit[1]
    out = None
    try:
        bars = anchored_bars(symbol)
        spot = None
        try:
            from .data_feed import fetch_spot
            spot = fetch_spot(symbol)
        except Exception:
            spot = None
        out = flow_from_bars(bars, spot, tol=(float(atr or 0.0) * TOL_ATR))
    except Exception:
        logger.warning("WEEKVWAP %s: unreadable.", symbol, exc_info=True)
        out = None
    _CACHE[symbol] = (time.time(), out)
    return out


def gate(direction: str, flow: "dict | None") -> "tuple[bool, str]":
    """(allowed, why). direction is 'bullish' for call debits, 'bearish' for puts."""
    if not flow:
        return False, "no weekly VWAP read (fewer than %d bars or the feed failed)" % MIN_BARS
    bull = direction == "bullish"
    fails = []
    if bull:
        if flow["side"] != "ABOVE":
            fails.append("spot %.2f not above the week's VWAP %.2f (%s, band %.2f)"
                         % (flow["spot"], flow["vwap_week"], flow["side"], flow["tol"]))
        if flow["slope_pct"] <= 0:
            fails.append("week's VWAP slope %+.3f%% not rising" % flow["slope_pct"])
    else:
        if flow["side"] != "BELOW":
            fails.append("spot %.2f not below the week's VWAP %.2f (%s, band %.2f)"
                         % (flow["spot"], flow["vwap_week"], flow["side"], flow["tol"]))
        if flow["slope_pct"] >= 0:
            fails.append("week's VWAP slope %+.3f%% not falling" % flow["slope_pct"])
    if fails:
        return False, "; ".join(fails)
    return True, ("the week's volume is paying up" if bull
                  else "the week's volume is paying down")


def describe(flow: "dict | None") -> str:
    if not flow:
        return "WEEKVWAP unreadable"
    return ("WEEKVWAP spot %.2f %s week VWAP %.2f (band %.2f), slope %+.3f%%/%d bars, "
            "%.0f%% bars above, %d bars over %d session(s)"
            % (flow["spot"], flow["side"], flow["vwap_week"], flow["tol"],
               flow["slope_pct"], SLOPE_BARS, 100 * flow["bars_above"],
               flow["bars"], flow["sessions"]))
