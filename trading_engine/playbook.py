"""Named, time-windowed entry strategies.

The engine used to run one structure — long ITM / short ATM — at every hour
of the session. That has a specific cost: a debit spread can never be worth
more than its width, so where the strikes sit relative to spot decides how
much of a move you actually capture. Measured on a $3-wide spread with QQQ
up $9 by expiry:

    deep ITM   +5%      ITM   +58%      ATM   +173%      OTM   +556%

A $9 day and a $3 day pay an ITM spread the same 58%. Widening the spread
does not help — a $9-wide ITM spread costs proportionally more and returns
18%. Capturing a bigger move means moving the strikes toward the money, not
making them wider. The tradeoff is real: ITM wins often and small, ATM wins
less often and large.

So structure is chosen by time of day, on the reasoning that the market's
behaviour differs by hour: the morning produces directional moves worth
paying for leverage on, and the midday lull produces chop where positive
theta and a high hit rate matter more.

Every window carries a NAME, recorded on the position and carried through to
TradeHistory, so performance can be attributed per strategy rather than
lumped together. Pruning is deliberately trivial: delete a window from
WINDOWS and the engine stops trading it.
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

# How the long leg sits relative to the ATM short strike.
ITM = "ITM"       # debit, long leg in the money  — high win rate, capped upside
ATM = "ATM"       # debit, long leg at the money  — lower win rate, larger upside
CREDIT = "CREDIT" # sell premium OTM — paid to open, profits as the spread decays


@dataclass(frozen=True)
class PlaybookWindow:
    name: str
    start: time
    end: time
    placement: str      # ITM, ATM (debit) or CREDIT
    width: float        # distance between the strikes, in dollars
    # Exit thresholds are per-strategy because the structures share no scale.
    # A debit position's return is a percentage of premium PAID; a credit
    # position's is a percentage of credit RECEIVED. So +50% means "roughly
    # doubled" for one and "decayed halfway to zero" for the other, and -100%
    # means total loss for one and the standard buy-it-back-at-twice-the-
    # premium stop for the other.
    take_profit_pct: float
    # None means "use the engine-wide value", which is what the environment
    # sets. Hardcoding a number here silently overrode every env override --
    # TRADING_STOP_LOSS_PCT was tuned three times in one session and reached
    # no trade, because thresholds_for preferred the literal in this table.
    stop_loss_pct: "float | None" = None
    risk_off_pct: "float | None" = None
    note: str = ""


# Ordered, non-overlapping. Times are ET.
#
# Deleting an entry here removes that strategy entirely — that is the intended
# way to retire one that is not earning its place.
WINDOWS = (
    PlaybookWindow(
        name="ATM_MOMENTUM",
        start=_env_time("TRADING_MOMENTUM_START", "09:45"), end=time(10, 15), placement=ITM, width=3.0,
        # Widest stop of the day. Measured over a month the average 5-minute
        # bar here spans $1.78 -- more than double the midday $0.83 -- and a
        # -20% stop on an ITM 3-wide is only a $0.65 move. The opening window
        # is a different volatility regime and a stop sized for the lull is
        # inside a single ordinary bar here.
        take_profit_pct=35.0, stop_loss_pct=-40.0, risk_off_pct=-25.0,
        note="Opening range resolved. Pay for leverage while a real directional "
             "leg is most likely; this is the window that can capture a big day. "
             "40% ARMS the trailing exit rather than selling. A 90-100% target "
             "sounds right and is effectively unreachable from a morning "
             "entry: doubling a $2-wide ATM spread needs QQQ up more than $6 "
             "with 3 hours left, against a typical DAILY range of $3.50-6.30. "
             "The position would simply ride to the force close. Arming low "
             "and trailing the 9 EMA gives the same uncapped upside from a "
             "target that actually fires.",
    ),
    PlaybookWindow(
        name="MORNING_DRIFT",
        start=time(10, 15), end=time(11, 30), placement=ITM, width=3.0,
        # $1.30 average bar — between the opening and the lull.
        take_profit_pct=35.0, stop_loss_pct=-30.0, risk_off_pct=-18.0,
        note="The opening leg is spent and the midday range has not formed. ATM "
             "rather than ITM so a second morning move is still worth catching, "
             "on the same 90% target. The least justified window of the four -- "
             "first candidate for removal if the scoreboard does not defend it.",
    ),
    PlaybookWindow(
        name="ITM_GRINDER",
        start=time(11, 30), end=time(13, 30), placement=ITM, width=3.0,
        # Quietest window of the session at $0.83 a bar, which is exactly what
        # a positive-theta ITM structure wants: the engine-wide -20% stop is
        # comfortably outside one bar here.
        take_profit_pct=30.0,
        note="Midday lull. Volume dries up and QQQ tends to consolidate, so a "
             "positive-theta structure that also pays when price sits still.",
    ),
    PlaybookWindow(
        name="AFTERNOON_CREDIT",
        start=time(13, 30), end=time(15, 0), placement=CREDIT, width=3.0,
        # 50% of the credit captured, per the strategy note. -100% is the
        # classic credit stop: buy it back for twice what you sold it for.
        take_profit_pct=50.0, stop_loss_pct=-100.0, risk_off_pct=-60.0,
        note="Theta is steepest in the last two hours and a debit spread is on "
             "the wrong side of it — it needs a move and there is little day "
             "left to get one. Selling premium turns that decay into the edge: "
             "this wins if QQQ falls, sits still, or moves moderately the wrong "
             "way, provided the short strike holds. A bullish read sells puts "
             "below spot, a bearish read sells calls above. Runs to 15:00 "
             "rather than 14:00 because a credit position WANTS less time left, "
             "which is exactly why the debit cutoff does not apply to it.",
    ),
)


# Which windows may OPEN a position, by name. Everything stays defined above
# so thresholds_for() can still price and exit a position opened under a
# window that is now switched off -- disabling a window must never orphan a
# live position's exit rules.
#
# Default is AFTERNOON_CREDIT alone, and that is a measured choice rather
# than a preference. Simulated over 60 sessions with the engine's own
# pricing and exit ladder, per day:
#
#     all four windows, 2 contracts     -18.91      max drawdown  2342
#     all four windows, 10 contracts    -35.17      max drawdown  9677
#     credit window only, 1 contract    +14.13      max drawdown   165
#     credit window only, 3 contracts   +57.04      max drawdown   639
#
# Every debit placement lost money: ITM -517, ATM -1358, OTM -1129 over 240
# morning trades each. Extending credit earlier to 11:30 also measured worse
# (+3428 against +8198), because that entry wins 67% of the time against 86%
# at 13:30 and it occupies the slot early.
#
# Set TRADING_ENABLED_WINDOWS to a comma-separated list to change this;
# "ALL" restores every window.
_enabled_raw = os.getenv("TRADING_ENABLED_WINDOWS", "AFTERNOON_CREDIT").strip()
ENABLED_WINDOWS = (
    frozenset(w.name for w in WINDOWS)
    if _enabled_raw.upper() == "ALL"
    else frozenset(n.strip() for n in _enabled_raw.split(",") if n.strip())
)


def window_for(now: Optional[datetime] = None) -> Optional[PlaybookWindow]:
    """The window covering `now`, or None outside all of them.

    None means no entry — it covers the opening warmup, anything past the
    final window's end, any gap left by removing a window, and any window
    switched off via TRADING_ENABLED_WINDOWS.
    """
    now = now or datetime.now(NY)
    t = now.time()
    for w in WINDOWS:
        if w.start <= t < w.end:
            return w if w.name in ENABLED_WINDOWS else None
    return None


def strikes_for(window: PlaybookWindow, atm_strike: float, bullish: bool) -> tuple[float, float]:
    """(long_strike, short_strike) for this window's placement.

    A bull call spread is long the lower strike and short the higher one; a
    bear put spread is the mirror. ITM placement puts the long leg in the
    money and the short leg at the money. ATM placement puts the long leg at
    the money and pushes the short leg out — cheaper, and it needs the move.
    """
    w = window.width
    if window.placement == ATM:
        return (atm_strike, atm_strike + w) if bullish else (atm_strike, atm_strike - w)
    return (atm_strike - w, atm_strike) if bullish else (atm_strike + w, atm_strike)


# How far out of the money the short leg sits on a credit vertical. Further
# out means a higher chance of expiring worthless but a smaller credit — the
# single knob trading win rate against payout.
OTM_OFFSET = float(os.getenv("TRADING_OTM_OFFSET", "3.0"))

# Volatility-adaptive alternative to the fixed offset above. The short strike
# sits SD_MULTIPLE standard deviations from spot, floored at OTM_OFFSET.
#
# A fixed distance is wrong on both tails: too close when volatility spikes
# (the strike gets breached) and needlessly far when it is quiet (the credit
# is not worth collecting). Anchored to SPOT rather than the moving average,
# because an SMA lagging below price drags a call strike toward the money --
# measured live, a nominal "4 standard deviation" placement off the SMA came
# out $1 CLOSER to spot than the plain fixed offset.
OTM_SD_MULTIPLE = float(os.getenv("TRADING_OTM_SD_MULTIPLE", "3.0"))


def otm_offset_for(sigma: float) -> float:
    """Distance from spot to the short strike, in dollars."""
    if sigma is None or sigma <= 0:
        return OTM_OFFSET
    return max(OTM_SD_MULTIPLE * sigma, OTM_OFFSET)


def credit_strikes_for(window: PlaybookWindow, atm_strike: float, bullish: bool,
                       sigma: "float | None" = None) -> tuple[float, float]:
    """(short_strike, long_strike) for an OTM credit vertical.

    Bullish sells puts BELOW spot; bearish sells calls ABOVE. The long leg
    sits a further `width` out and is what caps the loss — without it this
    would be a naked short option with unbounded risk.
    """
    w = window.width
    off = otm_offset_for(sigma)
    if bullish:
        short = atm_strike - off
        return short, short - w
    short = atm_strike + off
    return short, short + w


def thresholds_for(playbook_name: str, defaults: tuple) -> tuple:
    """(take_profit, stop_loss, risk_off) for the strategy that OPENED a
    position, matched on the window portion of the name.

    Looked up by name and not by the clock: a credit spread opened at 13:45
    must still be judged on credit thresholds at 15:00, and against debit
    thresholds it would read as catastrophically losing the moment it moved.
    """
    base = (playbook_name or "").split(":", 1)[0]
    for w in WINDOWS:
        if w.name == base:
            # Fall back per-field: a window only overrides what it actually
            # sets, so the environment governs everything else.
            return (
                w.take_profit_pct if w.take_profit_pct is not None else defaults[0],
                w.stop_loss_pct if w.stop_loss_pct is not None else defaults[1],
                w.risk_off_pct if w.risk_off_pct is not None else defaults[2],
            )
    return defaults


def take_profit_for(playbook_name: str, default: float) -> float:
    """The take-profit target for the strategy that OPENED a position.

    Looked up by name rather than by the current clock: a position opened in
    ATM_MOMENTUM is still an ATM structure at 13:00, and judging it against
    the ITM target that happens to be in force then would book it early for
    no reason. `default` covers positions opened before per-strategy targets
    existed, and any whose window has since been retired.
    """
    # Names carry a tier suffix ("ITM_GRINDER:RELAXED") so the scoreboard can
    # split them; the exit target belongs to the window, not the tier.
    base = (playbook_name or "").split(":", 1)[0]
    for w in WINDOWS:
        if w.name == base:
            return w.take_profit_pct
    return default
