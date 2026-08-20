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

def _env_float(var: str, default: "float | None") -> "float | None":
    """A float from the environment, or the default when unset or unparseable."""
    raw = os.getenv(var)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


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
    # How deep in the money the LONG leg sits, in dollars. None means "as deep
    # as the spread is wide", which puts the short leg exactly at the money --
    # the placement every measurement in this file was run on.
    #
    # Separating depth from width is what makes the two competing shapes one
    # parameter. At width 5, depth 5 is the deep-ITM spread (long 705 / short
    # 710 with QQQ at 710); depth 2 is the widely-quoted 60-delta long /
    # 20-delta short (long 708 / short 713), which costs less and has roughly
    # double the ceiling -- +110% against +55% priced at a 09:45 entry.
    #
    # The ceiling is not the expectancy. Swept over 16 bullish-stack mornings,
    # a long leg $2 in the money won 31% of the time and its result flipped
    # sign between sample halves, against 56% and a stable +25.25 for the deep
    # placement. Depth stays at the width by default for that reason;
    # TRADING_MORNING_LONG_DEPTH is there to test the other shape without a
    # code change, not because the evidence asks for it.
    long_depth: "float | None" = None
    # None means "use the engine-wide value", which is what the environment
    # sets. Hardcoding a number here silently overrode every env override --
    # TRADING_STOP_LOSS_PCT was tuned three times in one session and reached
    # no trade, because thresholds_for preferred the literal in this table.
    stop_loss_pct: "float | None" = None
    risk_off_pct: "float | None" = None
    # Credit windows only: take_profit_pct stops being the exit and becomes
    # the point where the trail ARMS, and this is the hard book.
    #
    # A credit spread's gain is bounded by the credit collected, which was
    # the argument for booking it at a fixed 50% and never trailing. Bounded
    # is not the same as worthless: 50% of the credit leaves the other half
    # on the table, and on 0DTE there is no next session to collect it in.
    # Observed live on 2026-08-20 — sold the 714/717 call spread for 0.405 at
    # 13:30, booked it at 0.197 (+51%, +62.43) at 14:09, and by 14:55 the
    # same spread was worth 0.114 (+72%, +87.42) with spot four dollars below
    # the short strike and 35 minutes still to run.
    #
    # 90 rather than 100 because the last tenth is the worst-paid: buying
    # back a spread at 0.04 costs the same two-legged bid-ask as buying it at
    # 0.20, and it is bought while short gamma into the close. What protects
    # the ride is the ratchet, not the target -- see the giveback logic in
    # nodes.execution_risk_agent.
    final_take_profit_pct: "float | None" = None
    # Which entry tiers may open this window. None means any tier.
    #
    # The tier ladder is global but the measured edge is not: morning ITM call
    # debit spreads made +18.74 a trade when the full bullish stack held at
    # entry and -4.49 when it did not, so letting a looser tier open this
    # window trades exactly the days that lose.
    entry_tiers: "frozenset[str] | None" = None
    # Refuse short entries. The tier ladder is symmetric -- CLEAN has a bear
    # side that opens a bear put spread -- but the measured edge is not.
    bullish_only: bool = False
    # Share of the DAY's risk budget one trade from this window may spend.
    #
    # The entry fraction says how much capital a trade deploys and nothing
    # about what it can lose, which are different questions once the windows
    # run different structures: 10% of equity buys 3 credit spreads stopping
    # at -$122, or 4 morning debit spreads stopping at -$199. Sizing that
    # looks even in dollars deployed is not even in dollars at risk.
    #
    # A share, not dollars, so the allocation follows the daily loss cap --
    # raise TRADING_MAX_DAILY_LOSS_PCT and every window scales with it.
    # 0.50 morning / 0.30 re-entry / 0.20 afternoon is the split as
    # requested; the windows do not compete for capital (one position at a
    # time, and the morning hands its slot over at 13:25), so this governs
    # per-trade size rather than reserving pools.
    #
    # Measured against the STOP, not the structural maximum. A credit
    # spread's true worst case is width-minus-credit -- $260 on the 3-wide
    # sold at 0.405 -- and budgeting against that at a 2% daily cap sizes
    # every credit trade to zero contracts. The stop is what the engine
    # intends to lose; the gap between it and the maximum is a 0DTE tail the
    # cap cannot price.
    risk_share: "float | None" = None
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
    # End the ride at this time instead of the force close, handing the single
    # position slot to a later window.
    #
    # Riding a morning winner to 15:45 blocks the 13:30 credit window
    # completely, because the engine holds one position at a time. Measured
    # over the 16 bullish-stack sessions:
    #
    #     morning rides to 15:45, credit never trades   +36.40 a day
    #     morning hands over at 13:25, credit trades    +80.87 a day
    #
    # The cut costs the morning trade 18 dollars (+36.40 -> +18.04, still
    # stable across halves) and buys a credit trade worth about 63. Holding
    # the better-looking single trade was costing more than it made.
    ride_until: "time | None" = None
    # Ignore the consecutive-loss halt. The dollar cap still governs this
    # window, so risk stays bounded.
    #
    # MAX_CONSECUTIVE_LOSSES was calibrated when every trade was the same size
    # and structure. It no longer is: three morning stops cost 3 x $37 = $111
    # against a $200 cap, yet would stand the session down and forfeit the
    # credit trade, which is the larger edge by roughly three to one. A cheap
    # loss should not be able to cancel an expensive opportunity.
    exempt_from_streak_halt: bool = False
    note: str = ""

    def allows_tier(self, tier: str) -> bool:
        return self.entry_tiers is None or tier in self.entry_tiers

    def allows_direction(self, bullish: bool) -> bool:
        return bullish or not self.bullish_only


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
        # $4 wide, not $3. ITM placement is (atm - width, atm), so a wider
        # spread also pushes the long leg deeper in the money, and depth is
        # what actually pays. Measured on the 16 bullish-stack mornings:
        #
        #     $3 wide, long $3 ITM   50% win  +18.04   halves +11.15 / +29.52
        #     $4 wide, long $4 ITM   56% win  +25.25   halves  +9.23 / +51.95
        #     $4 wide, long $2 ITM   31% win   +9.27   FLIPS
        #     $5 wide, long $2 ITM   31% win  +13.23   FLIPS
        #
        # The last row is the widely-quoted 60-delta long / 20-delta short
        # placement. It is the worst of the set here: the leverage comes from
        # a shallower long leg, and shallower is what loses. Note the caveat --
        # $4 buys its higher average with a wider gap between sample halves
        # (5.6x against 2.6x for $3), so some of the gain is a few large wins
        # rather than a steadier edge.
        # $5 wide, up from $4. The width buys a higher ceiling -- max gain
        # goes from +61% to +55% of a larger premium, $177 against $152 a
        # contract priced at a 09:45 entry -- and a deeper long leg with it,
        # since depth follows width by default and depth is what measured
        # well. Untested at this width: the sweep in the block below covers
        # $3, $4 and $5, but the $5 row was run at a $2 depth, not $5.
        start=time(10, 15), end=time(11, 30), placement=ITM, width=5.0,
        long_depth=_env_float("TRADING_MORNING_LONG_DEPTH", None),
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
        # Long only. CLEAN is symmetric and its bear side opens a bear put
        # spread, which was never measured before it shipped. Measured now, on
        # the 16 bearish-stack mornings at a 10:15 entry: ITM -15.52 a trade
        # at a 25% win rate, OTM -9.96 at a 6% win rate.
        #
        # It also fails the specification test that killed the dip tier. Moving
        # the judging bar five minutes at a time gives +4.13, -7.04, -15.52,
        # +18.23, +27.07 -- the sign is not stable, so there is no reliable
        # edge in either direction. The bullish side over the same sweep gives
        # +6.15, +9.12, +36.40, +34.50, +27.62: positive throughout.
        #
        # That also means +36.40 is the luckiest cell of the bullish sweep and
        # +9 to +36 is the honest range. The direction holds; the size does not.
        bullish_only=True,
        # One contract. At the global 0.10 this window would take 5, and a
        # single -30% stop would cost $274 against a $200 daily cap -- the
        # session would halt before 13:30 and forfeit the credit trade that
        # earns most of the money. At 0.03 the worst case is about $55, so
        # even three morning stop-outs leave the afternoon intact.
        # 0.04, up from 0.03, because the $5 spread costs $322 a contract and
        # 3% of a $10,130 account is $304 -- the window would have gone quiet
        # without a single log line saying why. The risk slice is unchanged
        # and still governs: 50% of the daily budget is $302, and one stop on
        # a $322 contract is $64, so the fraction is what binds here.
        entry_fraction=0.04,
        risk_share=0.50,
        ride_to_close=True,
        # 13:25, five minutes before AFTERNOON_CREDIT opens, so the slot is
        # genuinely free when the credit window looks for an entry.
        ride_until=time(13, 25),
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
        # $4 wide, up from $3. On a credit vertical the width is the LONG
        # leg's distance -- the short strike is placed by volatility and does
        # not move -- so this buys more credit for more capital at risk,
        # width-minus-credit per contract, and the risk allocation resizes
        # the position accordingly rather than quietly taking more exposure.
        start=time(13, 30), end=time(15, 0), placement=CREDIT, width=4.0,
        # 50 ARMS the trail rather than booking; 90 is where it books. The
        # fixed 50% exit was leaving the second half of the credit behind on
        # a 0DTE structure that has no next session to collect it in -- see
        # final_take_profit_pct above. -100% is the classic credit stop: buy
        # it back for twice what you sold it for.
        take_profit_pct=50.0, final_take_profit_pct=90.0, stop_loss_pct=-100.0, risk_off_pct=-60.0,
        risk_share=0.20,
        exempt_from_streak_halt=True,
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
    # Depth positions the LONG leg; width then places the short one a fixed
    # distance away. Depth defaults to the width, which puts the short leg at
    # the money and reproduces the placement every sweep in this file used.
    d = window.long_depth if window.long_depth is not None else w
    if bullish:
        long_strike = atm_strike - d
        return long_strike, long_strike + w
    long_strike = atm_strike + d
    return long_strike, long_strike - w


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


def ride_deadline(playbook_name: str) -> "time | None":
    """When the ride ends for the strategy that OPENED this position."""
    base = (playbook_name or "").split(":", 1)[0]
    for w in WINDOWS:
        if w.name == base:
            return w.ride_until
    return None


def risk_share_for(playbook_name: str, default: float) -> float:
    """Share of the day's risk budget one trade from this strategy may spend.

    Looked up by window name like thresholds_for, so a window that has not
    been given a share falls back to the engine-wide default rather than to
    whatever the clock is in now.
    """
    base = (playbook_name or "").split(":", 1)[0]
    for w in WINDOWS:
        if w.name == base and w.risk_share is not None:
            return w.risk_share
    return default


def final_take_profit_for(playbook_name: str) -> "float | None":
    """The hard book-it target for the strategy that OPENED this position,
    or None if that strategy exits at its take_profit_pct instead.

    Set means the window arms a trail at take_profit_pct and runs to here;
    the lookup is by name, like thresholds_for, so a position keeps the exit
    regime it was opened under even after the clock moves on.
    """
    base = (playbook_name or "").split(":", 1)[0]
    for w in WINDOWS:
        if w.name == base:
            return w.final_take_profit_pct
    return None


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
