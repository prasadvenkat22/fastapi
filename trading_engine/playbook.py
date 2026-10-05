"""The engine's one entry window: a 1-minute Bollinger band touch.

Section 261 (2026-10-05). The engine used to run six time-of-day windows
(ATM_MOMENTUM, MORNING_DRIFT, MORNING_PUT, MORNING_CREDIT, ITM_GRINDER,
AFTERNOON_CREDIT), each opened by its own entry tiers (CLEAN, ZONE, STRICT,
RELAXED, MOMENTUM, FADE, REJECT, TREND, THETA). Every replay in sections
251-255 found them without an edge and the operator retired them: the only
entry now is QQQ touching its 1-minute 20-period 2-SD band (section 260).
strategy_notes.txt keeps the record of what the old windows measured.

The window still carries a NAME, recorded on the position and carried to
TradeHistory, so /trading/playbook-performance can attribute results.
"""

import os
from dataclasses import dataclass
from datetime import datetime, time
from typing import Optional
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")


def _env_time(var: str, default: str) -> time:
    """HH:MM from the environment, so a session's start can be overridden
    without a code change and reverts by removing the variable."""
    raw = os.getenv(var, default)
    try:
        h, m = raw.split(":")
        return time(int(h), int(m))
    except Exception:
        h, m = default.split(":")
        return time(int(h), int(m))


def _env_float(var: str, default: "float | None") -> "float | None":
    """A float from the environment, or the default when unset or unparseable."""
    raw = os.getenv(var)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


# ONE STOP AND ONE TARGET FOR THE ENGINE (operator, 2026-10-04, section 256).
# The stop is a return on cost and defaults to -20 when unset or blank. The
# take-profit books outright at that return; blank leaves only the 20-SMA exit.
ENGINE_STOP_PCT = _env_float("TRADING_ENGINE_STOP_PCT", -20.0)
ENGINE_TAKE_PROFIT_PCT = _env_float("TRADING_ENGINE_TAKE_PROFIT_PCT", None)

# How the long leg sits relative to the ATM short strike.
ITM = "ITM"       # debit, long leg in the money, short leg at the money


@dataclass(frozen=True)
class PlaybookWindow:
    name: str
    start: time
    end: time
    placement: str      # ITM
    width: float        # distance between the strikes, in dollars
    stop_loss_pct: float = -20.0
    # Where the LONG leg sits, in dollars in the money. None = the width, which
    # puts the short leg at the money.
    long_depth: "float | None" = None
    note: str = ""

    def allows_tier(self, tier: str) -> bool:
        return tier == "TOUCH"

    def allows_direction(self, bullish: bool) -> bool:
        return True


BAND_TOUCH = PlaybookWindow(
    name="BAND_TOUCH",
    start=_env_time("TRADING_BAND_TOUCH_START", "09:45"),
    end=_env_time("TRADING_BAND_TOUCH_END", "15:00"),
    placement=ITM,
    width=_env_float("TRADING_BAND_TOUCH_WIDTH", 4.0),
    note="Fade a 1-minute Bollinger band touch back to the 20-SMA: calls at the "
         "lower band, puts at the upper.",
)

WINDOWS = (BAND_TOUCH,)


def window_for(now: Optional[datetime] = None) -> Optional[PlaybookWindow]:
    """The band-touch window if `now` is inside its hours, else None."""
    t = (now or datetime.now(NY)).time()
    return BAND_TOUCH if BAND_TOUCH.start <= t < BAND_TOUCH.end else None


def window_for_direction(bullish: bool, now: Optional[datetime] = None) -> Optional[PlaybookWindow]:
    """Same as window_for: the touch takes either side."""
    return window_for(now)


def strikes_for(window: PlaybookWindow, atm_strike: float, bullish: bool) -> tuple[float, float]:
    """(long_strike, short_strike): long leg in the money, short at the money.

    A bull call spread is long the lower strike and short the higher one; a
    bear put spread is the mirror.
    """
    w = window.width
    d = window.long_depth if window.long_depth is not None else w
    if bullish:
        long_strike = atm_strike - d
        return long_strike, long_strike + w
    long_strike = atm_strike + d
    return long_strike, long_strike - w


def window_by_playbook(playbook_name: str) -> "PlaybookWindow | None":
    """The window that OPENED this position, matched on the part before ':'."""
    base = (playbook_name or "").split(":", 1)[0]
    for w in WINDOWS:
        if w.name == base:
            return w
    return None


def thresholds_for(playbook_name: str, defaults: tuple) -> tuple:
    """(take_profit, stop_loss, risk_off) for the position's opening strategy.

    The stop is ENGINE_STOP_PCT for every engine position, including any row
    left by a retired window; take-profit and risk-off fall back to the
    caller's defaults.
    """
    stop = ENGINE_STOP_PCT if ENGINE_STOP_PCT is not None else defaults[1]
    return (defaults[0], stop, defaults[2])
