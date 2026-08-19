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
    # Which entry tiers may open this window. None means any tier.
    #
    # The tier ladder is global but the measured edge is not: morning ITM call
    # debit spreads made +18.74 a trade when the full bullish stack held at
    # entry and -4.49 when it did not, so letting a looser tier open this
    # window trades exactly the days that lose.
    entry_tiers: "frozenset[str] | None" = None
    # Per-window override of TRADING_ENTRY_FRACTION. None means the global.
    #
    # Needed because one fraction cannot serve both structures. A credit
    # vertical is sized on width-minus-credit and a debit one on premium, so
    # the same 0.10 buys 3 credit spreads (risking ~$780 against a -100% stop
    # of ~$123) but 5 debit spreads whose -30% stop is $274 -- more than the
    # whole daily loss cap. One morning stop-out would then halt the session
    # and forfeit the afternoon credit trade, which is the larger edge.
    entry_fraction: "float | None" = None
    # Let winners run to the force close: keep the stop, drop the take-profit,
    # trail and ratchet.
    #
    # Measured on the 16 bullish-stack mornings, ITM call debit spreads at a
    # 10:15 entry, per contract:
    #
    #     full exit ladder   +18.82 a trade   worst -54
    #     stop only          +36.40 a trade   worst -54
    #
    # Same worst case, nearly double the return, and consistent across sample
    # halves (+34.18 then +38.61). The mechanism is not subtle: an ITM 3-wide
    # cannot return more than about +64% in total, and booking it at +30%
    # gives up half the move. The 9 EMA trail that was supposed to let it run
    # instead fires on ordinary 5-minute noise.
    #
    # Deliberately NOT applied to credit windows, whose gain is bounded by the
    # credit collected -- there is no tail there to let run, and holding past
    # the target only risks giving it back.
    ride_to_close: bool = False
    note: str = ""

    def allows_tier(self, tier: str) -> bool:
        return self.entry_tiers is None or tier in self.entry_tiers


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
        # -20, not the -30 this window carried while it ran the full exit
        # ladder. Once the take-profit is removed the stop is the ONLY exit
        # before the force close, so its width stops being a noise question
        # and becomes the whole downside. Swept across the 16 bullish-stack
        # mornings, per contract:
        #
        #     -15   +11.93 a trade   worst  -47   halves  +1.34 / +22.53
        #     -20   +36.32 a trade   worst  -54   halves +34.18 / +38.46
        #     -30   +35.89 a trade   worst -150   halves  +6.43 / +65.36
        #
        # -20 and -30 earn the same average, but -30 costs three times as much
        # on its worst day and its halves disagree wildly, which is a result
        # resting on a couple of large recoveries rather than on the rule.
        # -15 is inside the noise and gets stopped out of trades that worked.
        #
        # take_profit_pct is retained for reference but is not consulted while
        # ride_to_close is set.
        take_profit_pct=35.0, stop_loss_pct=-20.0, risk_off_pct=-18.0,
        # CLEAN only. Measured at a 10:15 entry over 60 sessions, ITM call
        # debit spreads returned +18.74 a trade on the 16 days the full
        # bullish stack held (price above VWAP and the 20 SMA, 9 EMA above
        # the 20 SMA) and -4.49 on the other 44. CLEAN is that stack plus an
        # RSI band, so it is the tier that selects those days. Split in half
        # the good bucket held: +17.99 then +20.00, 50% win rate in both.
        # ATM and OTM flipped sign between halves and stay off entirely.
        entry_tiers=frozenset({"CLEAN"}),
        # One contract. At the global 0.10 this window would take 5, and a
        # single -30% stop would cost $274 against a $200 daily cap -- the
        # session would halt before 13:30 and forfeit the credit trade that
        # earns most of the money. At 0.03 the worst case is about $55, so
        # even three morning stop-outs leave the afternoon intact.
        entry_fraction=0.03,
        ride_to_close=True,
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
# MORNING_DRIFT is back alongside it, but only on CLEAN and only at one
# contract -- see its entry_tiers and entry_fraction above. Pooling every
# morning day hid that ITM works when the trend actually confirms; the
# restriction is what makes it tradeable rather than the window itself.
#
# Set TRADING_ENABLED_WINDOWS to a comma-separated list to change this;
# "ALL" restores every window.
_enabled_raw = os.getenv("TRADING_ENABLED_WINDOWS", "MORNING_DRIFT,AFTERNOON_CREDIT").strip()
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


def rides_to_close(playbook_name: str) -> bool:
    """Whether the strategy that OPENED this position lets winners run.

    Matched on the window portion of the name, like thresholds_for, so a
    position keeps the exit regime it was opened under even after the clock
    has moved into a different window.
    """
    base = (playbook_name or "").split(":", 1)[0]
    for w in WINDOWS:
        if w.name == base:
            return w.ride_to_close
    return False


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
