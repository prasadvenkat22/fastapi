"""US equity market holidays and early closes.

The engine decided whether the market was open from the weekday and the clock
alone. That is right four days in five and wrong on roughly ten sessions a
year -- on which it would run all 390 cycles against the previous session's
stale quotes, compute indicators from them, and be free to open a position.

Discovered 2026-09-05, two days before Labor Day.

DATES ARE LISTED RATHER THAN COMPUTED. A rule ("third Monday in February")
is shorter and hides the observed-date shifts: Independence Day 2026 falls on
a Saturday and the market closes on Friday the 3rd instead. An explicit list
is checkable against the exchange calendar by eye, which a rule is not.

EARLY CLOSES matter separately: the market closes at 13:00, so a 15:45
flatten never runs and every 0DTE position goes to expiry unmanaged. Those
dates are listed too, and close_time_for() is what a caller should use rather
than assuming 16:00.
"""
from datetime import date, time

# Full closures. Extend each December for the year ahead.
HOLIDAYS = {
    date(2026, 1, 1),    # New Year's Day
    date(2026, 1, 19),   # Martin Luther King Jr. Day
    date(2026, 2, 16),   # Presidents' Day
    date(2026, 4, 3),    # Good Friday
    date(2026, 5, 25),   # Memorial Day
    date(2026, 6, 19),   # Juneteenth
    date(2026, 7, 3),    # Independence Day observed (the 4th is a Saturday)
    date(2026, 9, 7),    # Labor Day
    date(2026, 11, 26),  # Thanksgiving
    date(2026, 12, 25),  # Christmas
}

# 13:00 ET closes.
EARLY_CLOSES = {
    date(2026, 7, 2),    # the day before Independence Day observed
    date(2026, 11, 27),  # the day after Thanksgiving
    date(2026, 12, 24),  # Christmas Eve
}

REGULAR_CLOSE = time(16, 0)
EARLY_CLOSE = time(13, 0)


def is_trading_day(d: date) -> bool:
    """Weekday, and not a full closure."""
    return d.weekday() < 5 and d not in HOLIDAYS


def close_time_for(d: date) -> time:
    """When the session ends. 13:00 on a half day, 16:00 otherwise."""
    return EARLY_CLOSE if d in EARLY_CLOSES else REGULAR_CLOSE


def minutes_to_close(now) -> float:
    """Minutes from `now` (a tz-aware NY datetime) to that session's close."""
    close = close_time_for(now.date())
    return ((close.hour - now.hour) * 60.0) + (close.minute - now.minute)
