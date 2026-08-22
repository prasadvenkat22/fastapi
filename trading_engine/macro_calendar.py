"""Scheduled macro events, and whether the engine should stand aside for one.

Nothing in the engine knew an FOMC day from any other day. The VIX gate is a
LEVEL -- it fires above 22.0 and a Fed day with the VIX at 18 passes it
untouched -- and the sentiment verdict gates BULLISH entries only, so the
afternoon credit spread could open in either direction minutes before a rate
decision. That is the exposure this module exists to remove.

The dates are the hard part and are deliberately not inferred. FOMC decision
days come from federalreserve.gov's published calendar. CPI releases are not
included: the BLS schedule is not machine-readable from here, and inventing
dates for a blackout is worse than having none, because a wrong date buys
false confidence on the day it matters. Populate TRADING_EVENT_DATES with
them -- and with anything else worth standing aside for.
"""

import logging
import os
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

NY = ZoneInfo("America/New_York")

# FOMC statement days, 2026. The decision lands at 14:00 ET, which is inside
# the credit window -- the reason an afternoon-only blackout is a distinct
# option from standing down for the whole session.
FOMC_2026 = (
    "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17",
    "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09",
)

# Anything else worth standing aside for: CPI and PPI prints, jobs reports,
# a scheduled geopolitical event. Comma-separated ISO dates.
_extra_raw = os.getenv("TRADING_EVENT_DATES", "")
EXTRA_EVENT_DATES = tuple(d.strip() for d in _extra_raw.split(",") if d.strip())

EVENT_DATES = frozenset(FOMC_2026 + EXTRA_EVENT_DATES)

# What a listed date does. Measured before being switched on -- see
# scripts/sweep.py events.
#
#   off        listed dates change nothing; the day is only logged
#   afternoon  no new entries from AFTERNOON_START on an event day, which is
#              the window a 14:00 decision actually sits in
#   day        no new entries at all on an event day
EVENT_BLACKOUT = os.getenv("TRADING_EVENT_BLACKOUT", "off").lower()

# When an afternoon blackout begins. 13:00 rather than 14:00: a credit spread
# opened at 13:30 is still open when the decision lands, so blocking only the
# minutes after the announcement protects nothing.
_raw_start = os.getenv("TRADING_EVENT_AFTERNOON_START", "13:00")
try:
    _h, _m = (int(p) for p in _raw_start.split(":"))
    AFTERNOON_START = time(_h, _m)
except ValueError:
    AFTERNOON_START = time(13, 0)


def is_event_day(when: "date | datetime | None" = None) -> bool:
    """Whether this session carries a scheduled macro event."""
    when = when or datetime.now(NY)
    day = when.date() if isinstance(when, datetime) else when
    return day.isoformat() in EVENT_DATES


def blackout_active(now: "datetime | None" = None) -> bool:
    """Whether new entries should be refused right now for a scheduled event.

    Returns False whenever the blackout is off, so a listed date still shows
    up in the logs without changing a single decision until the sweep says it
    should.
    """
    if EVENT_BLACKOUT == "off":
        return False
    now = now or datetime.now(NY)
    if not is_event_day(now):
        return False
    if EVENT_BLACKOUT == "day":
        return True
    if EVENT_BLACKOUT == "afternoon":
        return now.time() >= AFTERNOON_START
    return False


def describe(now: "datetime | None" = None) -> str:
    """One line for the cycle log, whether or not the blackout is armed."""
    now = now or datetime.now(NY)
    if not is_event_day(now):
        return ""
    state = "standing aside" if blackout_active(now) else f"blackout {EVENT_BLACKOUT}"
    return f"scheduled macro event today ({now.date().isoformat()}) — {state}"
