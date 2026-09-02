"""Fixed price levels, as distinct from moving statistics.

Why this exists
---------------
Every indicator the engine reads is a moving statistic: EMAs, VWAP, Bollinger
bands, MACD, RSI, ADX. All of them are recomputed from a trailing window, so
all of them move when price moves, and none of them is a place. A prior day's
high is a place. It sits at one number all session, other participants can see
the same number, and price either takes it or rejects it.

That is a genuinely different kind of information from anything already in the
state dict, which is the only argument for adding a twelfth reading to an
engine that already has eleven. It is not a better trend filter -- it is not a
trend filter at all.

What is computed
----------------
Five levels, from the SAME 5-day 5-minute series sma_agent already fetches, so
this costs no extra request:

  day_high / day_low     this session's regular-hours extremes so far
  prior_high / prior_low the previous session's regular-hours extremes
  prior_close            the previous session's last regular-hours close

and the readings that turn those into something a gate could gate on: signed
distance to each as a percentage of price, position within the day's range,
the overnight gap, and the change against the prior close -- the "prior day
change" that every outside description of this technique starts from.

Regular hours only, on both sessions. yfinance serves the extended session and
an overnight print is not a level anyone traded against; including pre-market
would put the "day low" at 04:15 on any morning with a futures dip.

What this does NOT do
---------------------
Gate anything, on its own. nodes.py holds the knobs (ZONE_*) and they default
off. Sections 27 onward record why: a reading enters this engine as a recorded
column, gets swept, and gates only if it survives both sample halves. The
distances are stored as NUMBERS beside every label so a threshold other than
the one chosen today can be tested later without re-collecting anything.
"""
import logging
from datetime import time as dtime

logger = logging.getLogger(__name__)

# Regular cash session. A level set in the extended session is not a level.
_OPEN, _CLOSE = dtime(9, 30), dtime(16, 0)

# How close counts as AT a level, in percent of price. 0.10% is about $0.70 on
# QQQ at 700 -- roughly two ticks of the spread the engine actually pays, and
# well inside a single 5-minute bar's range on an ordinary day.
#
# The label is a convenience for reading a log line. Every gate should prefer
# the raw distance columns, which is why they are all returned.
NEAR_PCT = 0.10


def _regular(bars, day):
    """One session's regular-hours bars."""
    return bars[[ts.date() == day and _OPEN <= ts.time() < _CLOSE for ts in bars.index]]


def _sessions(bars):
    """(today, prior) session dates present in the series, newest first.

    Prior is None on a one-day series -- the live engine fetches five days, but
    a caller that did not is a missing level, not an exception.
    """
    days = sorted({ts.date() for ts in bars.index})
    if not days:
        return None, None
    return days[-1], (days[-2] if len(days) > 1 else None)


def _pct(px, level):
    """Signed distance from price to a level, percent of price.

    Negative means price is BELOW the level. Sign is kept because "1% under
    the prior high" and "1% over it" are opposite situations and an absolute
    distance would call them the same.
    """
    if not px or level is None:
        return None
    return round((px - level) / px * 100.0, 3)


def read(bars, px: "float | None" = None) -> dict:
    """Zone state from an intraday bar series.

    `px` is the price to measure against -- pass the live spot where one is
    available, since a 5-minute close can be five minutes stale and a level
    test is exactly the situation where those five minutes decide the answer.
    Falls back to the last close.

    Returns every key with a None value on bad input rather than raising or
    returning {}. Callers write these into the state dict and log them, and a
    missing key reads as a code change while a None reads as what it is: this
    cycle had no prior session to measure against.
    """
    empty = {k: None for k in (
        "zone", "zone_extension", "day_high", "day_low", "day_open",
        "prior_high", "prior_low", "prior_close",
        "dist_day_high_pct", "dist_day_low_pct", "dist_prior_high_pct",
        "dist_prior_low_pct", "dist_prior_close_pct",
        "day_range_pos_pct", "gap_pct", "prior_change_pct",
        "minutes_since_day_low", "minutes_since_day_high",
        "bounce_off_low_pct", "fade_off_high_pct")}
    try:
        today, prior = _sessions(bars)
        if today is None:
            return empty
        tb = _regular(bars, today)
        if not len(tb):
            return empty

        px = float(px) if px else float(tb["Close"].iloc[-1])
        day_high = float(tb["High"].max())
        day_low = float(tb["Low"].min())
        day_open = float(tb["Open"].iloc[0])

        prior_high = prior_low = prior_close = None
        if prior is not None:
            pb = _regular(bars, prior)
            if len(pb):
                prior_high = float(pb["High"].max())
                prior_low = float(pb["Low"].min())
                prior_close = float(pb["Close"].iloc[-1])

        # Where in today's range price sits: 0 at the session low, 100 at the
        # session high. One number that says "buying the high" or "buying the
        # low" without needing a threshold at all.
        span = day_high - day_low
        range_pos = round((px - day_low) / span * 100.0, 1) if span > 0 else 50.0

        dists = {
            "dist_day_high_pct": _pct(px, day_high),
            "dist_day_low_pct": _pct(px, day_low),
            "dist_prior_high_pct": _pct(px, prior_high),
            "dist_prior_low_pct": _pct(px, prior_low),
            "dist_prior_close_pct": _pct(px, prior_close),
        }

        # Nearest level within NEAR_PCT, by absolute distance. Ties go to the
        # earlier key, which orders today's levels ahead of yesterday's --
        # today's are the ones being traded against right now.
        near, best = None, NEAR_PCT
        for key, label in (("dist_day_high_pct", "AT_DAY_HIGH"),
                           ("dist_day_low_pct", "AT_DAY_LOW"),
                           ("dist_prior_close_pct", "AT_PRIOR_CLOSE"),
                           ("dist_prior_high_pct", "AT_PRIOR_HIGH"),
                           ("dist_prior_low_pct", "AT_PRIOR_LOW")):
            d = dists[key]
            if d is not None and abs(d) <= best:
                near, best = label, abs(d)

        # Whether the session has left yesterday's range entirely. A day that
        # trades above the prior high is doing something different from one
        # oscillating inside it, regardless of where the EMAs are.
        if prior_high is None:
            extension = "UNKNOWN"
        elif px > prior_high:
            extension = "ABOVE_PRIOR_RANGE"
        elif px < prior_low:
            extension = "BELOW_PRIOR_RANGE"
        else:
            extension = "INSIDE_PRIOR_RANGE"

        # WHEN the extreme was set, not just where it is.
        #
        # A level is only a zone once it has held for a while. Price sitting
        # 0.2% above a low made sixty seconds ago is still making that low;
        # the same 0.2% above a low made forty minutes ago is a floor that has
        # been tested and has held. Without the clock those two readings are
        # identical, and they are opposite trades.
        last_ts = tb.index[-1]
        low_ts = tb["Low"].idxmin()
        high_ts = tb["High"].idxmax()
        since_low = max(0.0, (last_ts - low_ts).total_seconds() / 60.0)
        since_high = max(0.0, (last_ts - high_ts).total_seconds() / 60.0)

        out = {
            "minutes_since_day_low": round(since_low, 1),
            "minutes_since_day_high": round(since_high, 1),
            # How far price has LIFTED off the low / FALLEN from the high.
            # Distinct from dist_day_low_pct only in sign convention: these are
            # always >= 0 and read as "the size of the bounce", which is what a
            # trigger threshold is expressed in.
            "bounce_off_low_pct": round((px - day_low) / day_low * 100.0, 3) if day_low else None,
            "fade_off_high_pct": round((day_high - px) / day_high * 100.0, 3) if day_high else None,
            "zone": near or "MID_RANGE",
            "zone_extension": extension,
            "day_high": round(day_high, 2),
            "day_low": round(day_low, 2),
            "day_open": round(day_open, 2),
            "prior_high": round(prior_high, 2) if prior_high else None,
            "prior_low": round(prior_low, 2) if prior_low else None,
            "prior_close": round(prior_close, 2) if prior_close else None,
            "day_range_pos_pct": range_pos,
            "gap_pct": (round((day_open - prior_close) / prior_close * 100.0, 3)
                        if prior_close else None),
            "prior_change_pct": (round((px - prior_close) / prior_close * 100.0, 3)
                                 if prior_close else None),
        }
        out.update(dists)
        return out
    except Exception:
        logger.exception("Zone read failed — levels unavailable this cycle.")
        return empty
