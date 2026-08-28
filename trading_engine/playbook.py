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
    # Share of the peak this window's ratchet hands back before booking.
    # None means the engine-wide TRADING_TRAIL_GIVEBACK.
    #
    # Per-window because the number was inherited from the debit side and the
    # two structures want different things from it. Swept over 60 sessions on
    # the credit window, same structure and pricing in every arm:
    #
    #     15%   76 tr  89% win  +34.02/tr   halves +29.28/+38.75
    #     20%   76 tr  89% win  +34.65/tr   halves +30.35/+38.95
    #     30%   70 tr  89% win  +39.67/tr   halves +35.41/+43.93
    #     50%   66 tr  85% win  +40.59/tr   halves +38.83/+42.35
    #
    # 30% earns 14% more a trade at an identical win rate and an identical
    # worst trade, and both halves improve. The mechanism is in the exit mix:
    # trades reaching the 90% target rise from 11 to 17 while ratchet exits
    # fall from 36 to 24 -- a tight giveback was booking winners that had not
    # finished decaying. 50% keeps drifting that way but starts giving back
    # the win rate and widens the worst trade, so it is past the useful point.
    ratchet_giveback: "float | None" = None
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
    # The mirror. A window that may only take the SHORT side -- for a credit
    # structure that sells calls above a falling market, which is a different
    # trade from buying puts into one.
    bearish_only: bool = False
    # Require the session to be down at least this far, in percent from the
    # regular-hours open, before a SHORT entry is allowed.
    #
    # The distinction it draws is between a bearish signal and a bearish day.
    # Both morning short structures failed when they fired on any bearish
    # read: a put debit spread at 27% wins, a bear call spread at 38%. But the
    # sessions that actually fell hard mostly kept falling -- of the eight
    # that closed $9 or more down, six were still falling after 10:15. The
    # losses came from the many mild bearish mornings that stalled and
    # bounced, not from the severe ones.
    min_session_drop_pct: "float | None" = None
    # The bullish mirror: require the session to have been down at least this
    # far AT ITS WORST before a LONG entry is allowed.
    #
    # Different field and different reading from min_session_drop_pct above,
    # which gates shorts on where the session stands NOW. A reversal entry
    # cares where it has BEEN -- by the time CLEAN fires, price is back above
    # VWAP and both EMAs, so the current move is near zero on exactly the days
    # this is meant to select. It reads session_drawdown_pct.
    #
    # The thesis is the one already written into MORNING_CREDIT's note, from
    # the other side: MORNING DECLINES IN QQQ TEND TO REVERSE. That fact was
    # used to explain why two bearish structures fail. This is the bullish
    # half of the same observation, and until now nobody had tested it.
    min_session_drawdown_pct: "float | None" = None
    # Sell premium INTO STRENGTH: refuse a credit entry until the underlying
    # has risen this many dollars above its price at the window's start.
    #
    # A call credit spread sold at a fixed clock time places its short strike
    # against wherever price happens to be. Sold after a push up, the same
    # delta sits above a local high instead. Measured over 60 sessions on the
    # $2-wide, $1-OTM spread booking at 50%:
    #
    #     enter at 13:30            59 tr  85% win   +4.81/tr  halves +6.49/+3.19
    #     enter after a +$0.50 rise 37 tr  95% win  +16.33/tr  halves +23.78/+9.27
    #     enter after a +$1.00 rise 20 tr  95% win  +16.18/tr  halves +23.99/+8.36
    #
    # Same mechanism as the one credit finding that worked earlier in the
    # week: selling at an upper-band pierce beat selling at a fixed time.
    # Price that has just run is a better place to sell calls than price that
    # happens to be there when a clock strikes.
    #
    # The cost is trade count -- it only fires on days that rise after the
    # window opens, cutting 59 sessions to 37 or 20. Twenty trades is thin.
    min_rise_from_start: "float | None" = None
    #
    # Swept over 60 sessions on both morning short structures. The gate works
    # -- it separates a bearish signal from a bearish day -- and the two
    # structures answer it very differently:
    #
    #     BEAR CALL      any read  32 tr  38% win   -57.64/tr
    #                    -0.25%    20 tr  45% win   -57.04/tr
    #                    -0.50%    14 tr  50% win   -62.52/tr
    #                    -0.75%     9 tr  22% win   -89.22/tr
    #
    #     PUT DEBIT      any read  22 tr  32% win   -41.48/tr
    #                    -0.25%    14 tr  29% win  -122.59/tr
    #                    -0.50%     9 tr  33% win   -78.32/tr
    #                    -0.75%     6 tr  50% win  +166.52/tr   halves +395/-62
    #
    # Severity lifts the bear call's hit rate from 38% to 50% and never its
    # economics, because its winners cap at the credit while its losers run to
    # width-minus-credit. The put debit spread is the structure that scales
    # with the move, so it is the one that responds -- and at -0.75% it turns
    # positive and lifts the engine from +49.85 a day to +69.51.
    #
    # Not enabled: six trades, and the halves are +395.19 then -62.14. The
    # whole result sits in the first half. Left here because the DIRECTION is
    # now understood -- on a severe decline the move usually continues, and
    # only a debit structure can monetise that -- so this is the cell to
    # re-examine once more severe sessions accumulate.
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
    # A hand-over deadline for a window that does NOT ride. Same purpose as
    # ride_until and a separate field because the ride branch owns that one:
    # a position is closed at this time so the next window's slot is free.
    #
    # Without it a morning credit spread exits only on its target, its stop or
    # the 15:45 force close, so a quiet bearish morning parks it in the single
    # position slot all day and AFTERNOON_CREDIT -- the window carrying most
    # of the engine's edge -- never opens. That is the exact failure the
    # 13:25 handoff was measured to prevent: +36.40 a day holding the morning
    # against +80.87 handing over.
    close_by: "time | None" = None
    # Ignore the consecutive-loss halt. The dollar cap still governs this
    # window, so risk stays bounded.
    #
    # MAX_CONSECUTIVE_LOSSES was calibrated when every trade was the same size
    # and structure. It no longer is: three morning stops cost 3 x $37 = $111
    # against a $200 cap, yet would stand the session down and forfeit the
    # credit trade, which is the larger edge by roughly three to one. A cheap
    # loss should not be able to cancel an expensive opportunity.
    exempt_from_streak_halt: bool = False
    # A stop that defers to the trend, off by default.
    #
    # When set, the percentage stop only fires if the named trend test has
    # ALSO broken; while the trend holds, the position rides through the stop
    # level. The motivating case is 2026-08-27, where the morning stopped out
    # at 11:39 on the dip to 716.67 -- the session's local bottom -- and QQQ
    # ran to 720.53 by 13:15 without it.
    #
    #   "ema_cross"  the 9 EMA is still the right side of the 20 SMA. The
    #                structural reading of "the trend is still up".
    #   "ema9"       price is still the right side of the 9 EMA. Faster, so
    #                it releases the stop back sooner.
    #
    # The downside is NOT unbounded: a debit spread's loss is capped at the
    # premium paid, the ride deadline and force close still end the trade,
    # and the risk-off rule still owns its band when macro turns BAD.
    stop_defers_to_trend: "str | None" = None
    note: str = ""

    def allows_tier(self, tier: str) -> bool:
        return self.entry_tiers is None or tier in self.entry_tiers

    def allows_direction(self, bullish: bool) -> bool:
        if bullish:
            return not self.bearish_only
        return not self.bullish_only


# Ordered, non-overlapping. Times are ET.
#
# Deleting an entry here removes that strategy entirely — that is the intended
# way to retire one that is not earning its place.
WINDOWS = (
    PlaybookWindow(
        name="ATM_MOMENTUM",
        start=_env_time("TRADING_MOMENTUM_START", "09:45"),
        end=_env_time("TRADING_MOMENTUM_END", "10:15"), placement=ITM,
        width=_env_float("TRADING_MOMENTUM_WIDTH", 3.0),
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
        # End 12:30, not 11:30. Chain-priced over 60 sessions with the credit
        # window off, varying only this field:
        #
        #     entries until 11:30   21 tr  +121.41/tr  +2549 tot  halves +367.82/-102.60
        #     entries until 12:30   27 tr   +89.11/tr  +2406 tot  halves +171.49/ +12.62
        #     entries until 13:00   30 tr   +52.57/tr  +1577 tot  halves  +67.81/ +37.34
        #
        # 11:30 has the highest total and the worst claim to it. Its second
        # half is NEGATIVE -- the whole +2549 comes from the older half of the
        # sample, which by this file's own standard is a result about the
        # sample rather than about the rule. 12:30 gives up 143 dollars of
        # total, takes 29% more trades, and is the shortest window whose
        # halves both come out positive.
        #
        # That also answers the second-half decay this window showed at 11:30:
        # it was substantially an artefact of a window too short to re-enter.
        # A setup that re-fires at noon had nowhere to fire into.
        start=_env_time("TRADING_MORNING_START", "10:15"),
        end=_env_time("TRADING_MORNING_END", "12:30"), placement=ITM,
        width=_env_float("TRADING_MORNING_WIDTH", 6.0),
        # $6 wide with the long leg $2 in the money, which is close to the
        # ATM-long / OTM-short shape rather than the deep one this window
        # carried before. Judged per DAY with the credit window live, which
        # is the frame the decision belongs in -- the morning hands its slot
        # over at 13:25, so a placement that stops out early frees that slot
        # and one that rides does not:
        #
        #     $5 deep      +41.42/day   63% green   worst -646   halves +29.01/+53.83
        #     $5 long $2   +43.98/day   60% green   worst -736   halves +33.22/+54.73
        #     $6 long $2   +49.46/day   60% green   worst -775   halves +41.42/+57.49
        #     $4 deep      +39.62/day   62% green   worst -692
        #     $6 deep      +34.43/day   63% green   worst -669   halves +12.59/+56.27
        #
        # This reverses the deep placement that stood here, and the reversal
        # came from repairing the measuring tool rather than from new data.
        # Every earlier width and depth result was produced by a harness that
        # computed indicators from a single session's bars, never reset the
        # account between arms and charged no slippage. Fixing all three moved
        # this cell from worst to best.
        #
        # What it costs is on the same rows and is not small: 42% wins against
        # 52%, a worst day of -775 against -646, and three green days in five
        # instead of nearly two in three. The average is better because the
        # structure is uncapped $4 higher, so the sessions that trend pay for
        # the ones that stop out. On 24 morning trades that is a thin basis --
        # provisional, and the first thing to re-run when forward data grows.
        long_depth=_env_float("TRADING_MORNING_LONG_DEPTH", 2.0),
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
        take_profit_pct=_env_float("TRADING_MORNING_TAKE_PROFIT", 35.0),
        stop_loss_pct=_env_float("TRADING_MORNING_STOP_PCT", -20.0),
        risk_off_pct=_env_float("TRADING_MORNING_RISK_OFF_PCT", -18.0),
        # CLEAN only. Measured at a 10:15 entry over 60 sessions, ITM call
        # debit spreads returned +18.74 a trade on the 16 days the full
        # bullish stack held (price above VWAP and the 20 SMA, 9 EMA above
        # the 20 SMA) and -4.49 on the other 44. CLEAN is that stack plus an
        # RSI band, so it is the tier that selects those days. Split in half
        # the good bucket held: +17.99 then +20.00, 50% win rate in both.
        # ATM and OTM flipped sign between halves and stay off entirely.
        # NO drawdown gate, and this one was worth testing properly.
        #
        # The idea: playbook already records that morning declines in QQQ tend
        # to reverse, so take the CLEAN long only on days that had already
        # fallen. A crude harness liked it -- +55.45 a trade above a 1.0%
        # drawdown against -12.56 below it. Run through the real engine
        # (sweep.py daydrop), chain-priced:
        #
        #     no gate                27 tr  37% win   +89.11/tr  halves +171.49/  +12.62
        #     had been down >=0.30%  21 tr  33% win   +49.13/tr  halves  -24.28/ +115.86
        #     had been down >=0.50%  15 tr  27% win   -54.37/tr  halves -234.49/ +103.23
        #     had been down >=0.70%   6 tr  50% win  +455.62/tr  halves -486.17/+1397.40
        #     had been down >=0.80%   5 tr  60% win  +615.93/tr  halves -556.27/+1397.40
        #     had been down >=1.00%   2 tr 100% win +1422.78/tr
        #
        # Read the ROW COUNT before the average. The spectacular cells are two
        # to six trades, and their halves disagree by nearly two thousand
        # dollars -- one or two enormous winners, not an edge. The cells with
        # enough trades to mean anything are WORSE than no gate, and 0.50% is
        # outright negative while 0.30% and 0.70% are positive. A real
        # threshold effect is monotone; this alternates, which is what noise
        # looks like when it is sliced finely enough.
        #
        # The crude harness that liked it scored all trades at -2.10 where the
        # engine scores +89.11 -- it was splitting a P&L it could not
        # reproduce, because it had no ratchet. That is the lesson worth
        # keeping: a filter tested on a harness that misses the exit logic is
        # measuring the harness.
        #
        # min_session_drawdown_pct and state["session_drawdown_pct"] stay,
        # wired and unused, so the next person can re-run this in one line
        # when the sample is bigger.
        min_session_drawdown_pct=None,
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
        # 0.08. Swept over 60 sessions with equity compounding, the daily cap
        # live and the tail cap enforced:
        #
        #      4%  +66.65 a day   worst -118.06
        #      8%  +70.54 a day   worst -230.80
        #     12%  +80.01 a day   worst -318.95
        #     20%  +80.12 a day   worst -495.25   (tail cap binds, no gain)
        #
        # 12% earns the most and is not chosen, because this window's whole
        # record is nine trades. Tripling the worst day to buy 20% more from
        # the least-sampled part of the engine is sizing a thin edge as though
        # it were the credit window's 76-trade one. 8% buys a second contract
        # on cheaper entries and stops there.
        # None -- this window deploys the same share of capital as every
        # other, set by TRADING_ENTRY_FRACTION. Chosen on 2026-08-22 to size
        # the morning like the afternoon rather than at the third of it the
        # sweep had settled on.
        #
        # The override it replaces was 0.08, and the evidence for it was
        # real: 4% +66.65/day, 8% +70.54, 12% +80.01, 20% +80.12, against a
        # worst day that ran -118, -231, -319, -495 across the same four. It
        # was held to 8% because this window's record is 24 trades where the
        # credit window's is 76, and because 12% bought 20% more return for
        # nearly three times the worst day. That sweep also predates both the
        # harness repairs and the placement change, so it is being re-run.
        entry_fraction=None,
        risk_share=0.50,
        ride_to_close=True,
        # 13:25 — and it is no longer a handoff. AFTERNOON_CREDIT is out of
        # TRADING_ENABLED_WINDOWS, so nothing is waiting for the slot; this is
        # now a plain midday exit, and it earns its place on its own.
        #
        # Measured with the 12:30 window end, chain-priced (sweep.py handoff):
        #
        #     hand over 13:25, credit trades      +3.19/day   halves +35.51/-29.13
        #     ride to 15:45, credit trades        +8.29/day   halves +80.72/-64.15
        #     ride to 15:45, credit OFF          +53.14/day   halves +142.22/-35.95
        #     exit at 13:25, credit OFF          +40.10/day   halves +74.31/ +5.89
        #
        # Riding wins on total by $782 over 60 sessions and loses on the only
        # test this file trusts: its second half is NEGATIVE, so the extra
        # comes from the older half of the sample. The 13:25 exit is the only
        # row of the four whose halves both come out positive. Same standard
        # that chose a 12:30 window end over 11:30 for less total money.
        #
        # An earlier run of this sweep, taken at the 11:30 window end, put
        # riding ahead on both counts (+44.15 against +42.49). Extending the
        # window to 12:30 reversed it: later entries have less room before
        # 13:25, so what rides past it is a different, worse-selected set of
        # trades. The two settings interact and have to be swept together.
        #
        # If AFTERNOON_CREDIT is ever re-enabled this stays exactly as it is —
        # it goes back to being a handoff and is needed more, not less.
        ride_until=time(13, 25),
        note="The opening leg is spent and the midday range has not formed. ATM "
             "rather than ITM so a second morning move is still worth catching, "
             "on the same 90% target. The least justified window of the four -- "
             "first candidate for removal if the scoreboard does not defend it.",
    ),
    PlaybookWindow(
        # Sell calls above a morning that is going down.
        #
        # Built because the engine has nothing for a bad morning. MORNING_DRIFT
        # is long-only and its CLEAN gate will not fire into a decline, so a
        # falling session is simply not traded until 13:30 -- and the obvious
        # filler, a put debit spread, is the structure that measured 27% wins
        # at -50.62 a trade.
        #
        # This is not that trade. A bear call spread wins if price merely fails
        # to rise, where a put debit spread needs the decline to continue, and
        # the two behave nothing alike on a day that stops falling and drifts.
        #
        # It is OFF because it was measured and it loses. Over 60 sessions,
        # 32 trades, with the 13:25 handoff in place:
        #
        #     38% win   -57.64 a trade   -1844 total   halves -37.95 / -77.32
        #     exits: 14 stop-loss, 13 ratchet, 4 handoff, 1 target
        #
        # Adding it takes the engine from +49.85 a day to +19.15. Negative in
        # both halves and worse in the second, so this is not a thin-sample
        # verdict that better data might reverse.
        #
        # The reason is the same one that killed the morning put debit spread,
        # and that is the part worth keeping: MORNING DECLINES IN QQQ TEND TO
        # REVERSE. A put debit spread needs the fall to continue and is bled by
        # the bounce; a bear call spread needs the strike to hold and is run
        # over by it. Two structures, opposite exposures, one fact about the
        # instrument. 2026-08-21 is the shape exactly -- down into 10:15, then
        # 709.40 to 715 by 11:50.
        #
        # The arithmetic leaves no room for a 38% win rate either: about $59 of
        # credit against $341 of structural risk, on a strike that survives 48%
        # of sessions from 10:15 against 92% from 13:30.
        #
        # So the engine's answer to a bad morning is to wait and sell calls at
        # 13:30. Sitting out is not a gap in the strategy; it is the strategy
        # declining a bet it has measured.
        name="MORNING_CREDIT",
        start=_env_time("TRADING_MORNING_CREDIT_START", "10:15"),
        end=_env_time("TRADING_MORNING_CREDIT_END", "11:30"), placement=CREDIT,
        width=_env_float("TRADING_MORNING_CREDIT_WIDTH", 4.0),
        take_profit_pct=50.0, final_take_profit_pct=90.0,
        stop_loss_pct=-100.0, risk_off_pct=-60.0,
        risk_share=0.50, ratchet_giveback=0.30,
        bearish_only=True,
        close_by=time(13, 25),
        exempt_from_streak_halt=True,
        note="The bad-morning counterpart to MORNING_DRIFT. Sells the side the "
             "market is moving away from rather than buying the direction it "
             "is moving in.",
    ),
    PlaybookWindow(
        name="ITM_GRINDER",
        start=_env_time("TRADING_GRINDER_START", "11:30"),
        end=_env_time("TRADING_GRINDER_END", "13:30"), placement=ITM,
        width=_env_float("TRADING_GRINDER_WIDTH", 3.0),
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
        # 14:00, not 13:30. Swept chain-priced over 60 sessions at one
        # contract, unconditional entry so placement is the only variable
        # (scratch/closer.py, 2026-08-24). Per trade, net of friction:
        #
        #     entry  short  wing    n  credit  win%   gross   NET
        #     13:30    +$2    $4   59    0.28   66%   +0.70  -10.70
        #     14:00    +$2    $4   59    0.21   64%   +2.31   -9.09   <- best
        #     14:30    +$2    $4   59    0.16   46%   -2.43  -13.83
        #
        # READ THE SIGN. The best cell here is the LEAST BAD, not a winner.
        # Every afternoon premium-selling variant measured on chain prices
        # loses: this window's own 3-sigma placement at all six widths, this
        # fixed-offset grid at three entry times and three wings, and the
        # condor at three offsets. The gross edge peaks near $2.31 a contract
        # against a round-trip cost of at least $3.40 -- and that floor is
        # tighter than any quote observed on this chain. It is not a tuning
        # problem, and moving the entry to 14:00 buys about $1.60 a trade of
        # a loss that stays a loss.
        start=_env_time("TRADING_CREDIT_START", "14:00"),
        end=_env_time("TRADING_CREDIT_END", "15:00"),
        placement=CREDIT,
        width=_env_float("TRADING_CREDIT_WIDTH", 4.0),
        # 50 ARMS the trail rather than booking; 90 is where it books. The
        # fixed 50% exit was leaving the second half of the credit behind on
        # a 0DTE structure that has no next session to collect it in -- see
        # final_take_profit_pct above. -100% is the classic credit stop: buy
        # it back for twice what you sold it for.
        take_profit_pct=_env_float("TRADING_CREDIT_TAKE_PROFIT", 50.0),
        final_take_profit_pct=_env_float("TRADING_CREDIT_FINAL_TAKE_PROFIT", 90.0),
        # -600%, not -100%. THE STOP WAS THE WRONG SHAPE FOR THIS STRUCTURE.
        #
        # -100 came from the debit side, where it means "the premium paid is
        # gone" and is a real limit. On a CREDIT position -100% means the
        # spread DOUBLED -- and doubling a sixteen-cent credit is thirty-two
        # cents, which a 67-cent move in QQQ produces without ever touching
        # the short strike.
        #
        # That is not a hypothetical. Live on 2026-08-25 the engine sold
        # 711/715 for 0.16 at 14:34 and bought it back at 0.39 at 14:46 for
        # -$23. QQQ's high for the rest of the session was 710.67 against a
        # 711 short strike: the spread expired worthless. Held, it pays
        # +$14.60. The stop cost $39 on a day the underlying fell $3.64 --
        # the exact day this structure exists for.
        #
        # A credit spread's loss is ALREADY capped at width-minus-credit by
        # the long leg. The stop does not lower that ceiling; it converts
        # unrealised noise into realised loss. Swept chain-priced through the
        # engine, 60 sessions:
        #
        #     -100% (was)   16% win   26/67 stopped   -47.14/tr   worst -1020
        #     -300%         19% win    6/59 stopped   -44.24/tr   worst -1020
        #     -400%         20% win    4/59 stopped   -36.52/tr   worst  -680
        #     -600%         22% win    3/59 stopped   -30.14/tr   worst  -340
        #
        # Monotone in both P&L and worst day, and at -600% the stop fires 3
        # times in 59 -- which is the intent: let the spread expire worthless
        # rather than booking a loss on a wiggle.
        #
        # IT DOES NOT MAKE THE WINDOW PAY. -30 a trade is still -30 a trade.
        # A breach-based stop was also tested (exit when spot reaches the
        # short strike) and measured WORSE than the percentage stop: a
        # breached spread often comes back, and stopping realises the loss at
        # its worst moment.
        stop_loss_pct=_env_float("TRADING_CREDIT_STOP_PCT", -600.0),
        risk_off_pct=-60.0,
        # None by default -- the measurement is 20-37 trades and needs a
        # forward record before it becomes the standing behaviour. Set
        # TRADING_CREDIT_MIN_RISE=0.50 to trade it.
        min_rise_from_start=_env_float("TRADING_CREDIT_MIN_RISE", None),
        # 0.50. Swept over 60 sessions with equity compounding, the daily cap
        # live and indicators warmed over five sessions as the live feed warms
        # them: +57.98 a day at 20%, +104.27 at 35%, +108.30 at 50%, and
        # identical at 80% because the capital fraction takes over there. The
        # worst day is -606.28 at every share, because the daily cap is what
        # bounds it rather than the slice.
        #
        # An earlier run of this sweep put the plateau at 35%. It was reading
        # indicators computed from a single session's bars, so the morning
        # window traded 9 times across 60 sessions instead of 23 and the
        # credit window saw a different set of days entirely.
        risk_share=0.50, ratchet_giveback=0.30,
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


def window_for_direction(bullish: bool, now: Optional[datetime] = None) -> Optional[PlaybookWindow]:
    """The window covering `now` that will accept this direction.

    WINDOWS was documented as non-overlapping and the plain window_for()
    returns the first match, which is fine while each hour has one owner. It
    stops being fine the moment two windows share an hour and differ only by
    the side they take -- a long-only debit window and a short-only credit one
    covering the same morning. Then the direction is part of the lookup, not
    something checked afterwards.
    """
    now = now or datetime.now(NY)
    t = now.time()
    fallback = None
    for w in WINDOWS:
        if not (w.start <= t < w.end) or w.name not in ENABLED_WINDOWS:
            continue
        if w.allows_direction(bullish):
            return w
        fallback = fallback or w
    # Nothing takes this side right now. Return the window that owns the hour
    # anyway, so the caller logs DIRECTION_NOT_ALLOWED rather than "no window".
    return fallback


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
#
# $2, down from $3. Swept chain-priced over 60 sessions at one contract with
# a $4 wing, unconditional entry so placement is the only variable
# (scratch/offset.py, 2026-08-24). Net of friction, per trade:
#
#     14:00 entry            n   avg off  credit  win%    NET     halves
#       fixed $2            59      2.00    0.21   64%   -9.09   -12.73/ -5.56
#       fixed $3            59      3.00    0.08   10%  -11.85   -13.78/ -9.99
#       max(3.0*sd, $3) <-  53      3.18    0.07    8%   -9.69    -9.59/ -9.78
#       max(3.0*sd, $2)     53      2.62    0.14   45%   -5.80    -6.57/ -5.05
#       max(2.0*sd, $2)     55      2.17    0.19   67%   -4.86    -5.46/ -4.28
#       max(1.5*sd, $2)     57      2.09    0.20   67%   -5.92    -6.18/ -5.67
#
# The row marked <- is what shipped before this. Two things came out of it.
# The volatility adaptation IS worth keeping -- every sigma-placed row beats
# the fixed row at the same floor -- but 3 sigma was too far out, collecting
# $0.07 and winning 8% of the time. And the halves finally agree at
# max(2.0*sd, $2), which none of the wider placements manage.
#
# It is still a loss. See the note on AFTERNOON_CREDIT's start time: nothing
# measured on chain prices makes this window positive, and this change takes
# it from about -9.7 to -4.9 a trade rather than to profit.
OTM_OFFSET = float(os.getenv("TRADING_OTM_OFFSET", "2.0"))

# Volatility-adaptive alternative to the fixed offset above. The short strike
# sits SD_MULTIPLE standard deviations from spot, floored at OTM_OFFSET.
#
# A fixed distance is wrong on both tails: too close when volatility spikes
# (the strike gets breached) and needlessly far when it is quiet (the credit
# is not worth collecting). Anchored to SPOT rather than the moving average,
# because an SMA lagging below price drags a call strike toward the money --
# measured live, a nominal "4 standard deviation" placement off the SMA came
# out $1 CLOSER to spot than the plain fixed offset.
# 2.0, down from 3.0 — see the sweep table on OTM_OFFSET above, which moved
# both knobs together because they only mean anything as a pair.
OTM_SD_MULTIPLE = float(os.getenv("TRADING_OTM_SD_MULTIPLE", "2.0"))


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


def close_deadline(playbook_name: str) -> "time | None":
    """When a non-riding window must hand its slot over, if it must."""
    base = (playbook_name or "").split(":", 1)[0]
    for w in WINDOWS:
        if w.name == base:
            return w.close_by
    return None


def ride_deadline(playbook_name: str) -> "time | None":
    """When the ride ends for the strategy that OPENED this position."""
    base = (playbook_name or "").split(":", 1)[0]
    for w in WINDOWS:
        if w.name == base:
            return w.ride_until
    return None


def stop_trend_guard_for(playbook_name: str) -> "str | None":
    """Which trend test, if any, must also break before this window's stop fires."""
    base = (playbook_name or "").split(":", 1)[0]
    for w in WINDOWS:
        if w.name == base:
            return w.stop_defers_to_trend
    return None


def ratchet_giveback_for(playbook_name: str, default: float) -> float:
    """How much of its peak a strategy hands back before the ratchet books."""
    base = (playbook_name or "").split(":", 1)[0]
    for w in WINDOWS:
        if w.name == base and w.ratchet_giveback is not None:
            return w.ratchet_giveback
    return default


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
