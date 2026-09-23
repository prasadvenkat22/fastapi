"""Positions the engine did not open, managed by the engine's own rules.

Why this exists
---------------
2026-09-02: a manual QQQ 700/708 spread was opened at 09:43 and ran unmanaged
all session while RECONCILE logged an ERROR once a minute and did nothing
else. Later the same day a manual 20-lot 709 credit spread peaked at +152.50
and closed at -657.50 with no rule watching it. Neither loss counted toward
the daily loss limit or the consecutive-loss breaker, because both read
TradeHistory and TradeHistory only records trades the ENGINE closed. The
account's real risk was invisible to every circuit breaker it had.

RECONSTRUCTED FROM ORDERS, NOT FROM POSITIONS
---------------------------------------------
The position list says what is held. Only the orders say how it got there,
and two things this module needs live only there:

  PAIRING. Nine long calls against nine short calls across five strikes admit
  several readings. Pairing by strike is a guess -- and the engine's own
  naked-leg check made exactly that guess on 2026-09-02 and called a fully
  covered book naked. A multileg order states which legs were put on together.

  ENTRY PRICE. Tradier averages cost_basis across contracts bought and sold
  under one position id: a 700 leg read 3540.00, then 4251.92, at an unchanged
  quantity of 5. Fill prices do not drift. Every rule below is expressed as a
  RETURN, and a return is meaningless without a true entry price. The previous
  version of this module measured value as a percent of spread WIDTH precisely
  because it could not trust the basis -- fine for a report, useless as a
  trigger.

WHAT IT DOES
------------
Marks each reconstructed structure, applies the same ladder the engine applies
to its own positions, and -- only when MANAGE_ORPHANS is set -- acts on it.
When a structure disappears from the account it books a TradeHistory row, so
manual results reach the daily loss limit and the consecutive-loss breaker
like any other trade.

THE STOP IS STRUCTURE-AWARE, and it has to be. A -25% stop is right for a
debit spread and absurd for a credit one, where the return is measured against
the credit collected and -100% is an ordinary bad day. Applying one number to
both would close every credit spread within minutes. Debit and credit get
their own, matched to what the playbook uses for the engine's own windows.

INTENT IS THE PART THIS CANNOT SEE. On 2026-09-02 a -25% stop would have
closed the credit book hours before the operator chose to ride it to 15:45.
MANAGE_ORPHANS is therefore opt-in and off by default, and MANAGE_UNDERLYING
narrows it further to the engine's own symbol.
"""
import json
import logging
import os
import time
from datetime import datetime, timezone

from . import tradier_orders

logger = logging.getLogger(__name__)

# Watch orphans at all. Off leaves the old behaviour: a RECONCILE line, and
# nothing else.
WATCH_ORPHANS = os.getenv("TRADING_WATCH_ORPHANS", "false").lower() == "true"

# ACT on them. Separate from watching on purpose -- see the intent paragraph
# above. Watching costs nothing and can only inform; acting closes positions a
# human opened for reasons the engine cannot read.
MANAGE_ORPHANS = os.getenv("TRADING_MANAGE_ORPHANS", "false").lower() == "true"
# ... and only in these underlyings, comma separated. Empty means every symbol.
#
# A LIST rather than one symbol: the account holds spreads on several names at
# once, and "manage the engine's own underlying" was too narrow the first
# morning it ran. Naming them explicitly rather than defaulting to everything,
# because the failure modes are asymmetric -- forgetting to add a symbol leaves
# a position watched but unmanaged and visible in the log, while managing a
# name by accident closes something the operator never offered up.
#
# TRADING_ORPHAN_UNDERLYING SPLITS THIS FROM THE NEWS LIST (2026-09-09).
# TRADING_MANAGE_UNDERLYING is read in two places that want different answers:
# here, to decide what the engine may CLOSE, and nodes._tracked_symbols(), to
# decide which names get per-ticker news scraping. They were the same variable,
# so taking a name out of engine management also silently stopped its news --
# and the operator asking for the first would never guess they had done the
# second.
#
# Falls back to TRADING_MANAGE_UNDERLYING when unset, so existing deployments
# behave exactly as before.
# SET BUT EMPTY MEANS EVERY SYMBOL, which is different from unset.
# `or` cannot express that -- an empty string is falsy and would silently fall
# back to the news list, so "watch anything I open" would quietly become
# "watch these eleven names". None means unset; "" means all.
_orphan_syms = os.getenv("TRADING_ORPHAN_UNDERLYING")
if _orphan_syms is None:
    _orphan_syms = os.getenv("TRADING_MANAGE_UNDERLYING", "QQQ")
MANAGE_UNDERLYING = {s.strip().upper() for s in _orphan_syms.split(",") if s.strip()}

# The ladder. Same shape as the engine's own, same variables where they are
# genuinely the same question.
# ONLY WHAT EXPIRES TODAY.
#
# The ladder is built for 0DTE: a -25% stop, a 90% ceiling and a 15:45 flatten
# all assume the position has hours to live, not days. Applied to a spread
# expiring later they are simply wrong -- the force close would flatten a
# Friday position on Wednesday for no reason, and a stall would book a
# multi-day thesis on one quiet afternoon.
#
# So the scope is the EXPIRY, not the symbol. A name is managed on the day its
# contracts expire and left alone before that, which is what makes one rule
# correct for every position rather than a list that has to be maintained.
ORPHAN_TODAY_ONLY = os.getenv("TRADING_ORPHAN_TODAY_ONLY", "true").lower() == "true"

# ACT ON NOTHING THAT IS NOT EXPIRING TODAY.
#
# ORPHAN_TODAY_ONLY above already scopes the STOP, the STALL and the FLATTEN
# to expiry day. Two rules deliberately stayed outside that scope: the CEILING,
# on the argument that 90% of maximum has the same tiny upside left whenever it
# expires, and the LATER-STALL, which exists specifically for multi-day
# positions.
#
# That argument is sound and it is still not what is wanted here. A position
# with a week to run is a DIFFERENT TRADE: it has time to recover from a move
# that would be terminal on expiry day, its mark is dominated by time premium
# that has not begun to decay, and the later-stall cannot even arm until the
# mark shows a gain -- which on a deep ITM weekly will not happen for days.
# Acting on it early converts a thesis with a week left into a same-day
# decision.
#
# Observed 2026-09-04: a SNDK 1600/1700 expiring the following Friday sat at
# full intrinsic (+3,825 at expiry) marking -0.7%, with a ceiling at 43% that
# could fire on a mark the position had no reason to reach yet.
#
# With this on, anything not expiring today is REPORTED and never acted on.
ORPHAN_ACT_EXPIRY_DAY_ONLY = os.getenv(
    "TRADING_ORPHAN_ACT_EXPIRY_DAY_ONLY", "false").lower() == "true"

# THE ONE RULE A LATER EXPIRY GETS: A PROFIT TARGET, AS A FRACTION OF WIDTH.
#
# ORPHAN_ACT_EXPIRY_DAY_ONLY leaves a weekly completely unmanaged -- no stop,
# no stall, no flatten -- which is right for its DOWNSIDE. A position with
# five days left has time to recover from a move that would be terminal on
# expiry day, and section 88 measured every stop and stall variant as a tax.
#
# But it also leaves the UPSIDE unattended, and there the 0DTE reasoning does
# not carry. Section 88's finding -- no profit target beats holding -- rests
# entirely on a mark that cannot converge to intrinsic before expiry. On a
# 0DTE spread that convergence happens in the last minutes. On a WEEKLY it
# happens over days: a spread whose underlying runs well past its short strike
# on a Tuesday can genuinely be sold near its width on Wednesday, because
# there is no longer meaningful premium in either leg.
#
# So a later expiry gets exactly one rule, and it only ever sells a WINNER:
#
#     mark >= width x this fraction   ->  close
#
# A FRACTION OF WIDTH, not of maximum return, and the difference is real
# money: on a 100-wide SNDK spread bought at 61.75, 90% of width is 90.00 and
# 90% of max return is 96.18. Section 88 recorded getting these two confused.
#
# UNMEASURED, and honestly so. It cannot be measured on the data in hand --
# every logged series is 0DTE, where the convergence this rule depends on
# never happens. Its case is structural rather than empirical, and the
# argument above is the whole of it. Next week's logs are what will settle it.
#
# 0 disables. Applies ONLY to positions not expiring today; a 0DTE position
# keeps the ordinary ladder.
ORPHAN_LATER_TARGET_PCT = float(
    os.getenv("TRADING_ORPHAN_LATER_TARGET_PCT", "0") or 0)

# ACCOUNT FLOOR: FLATTEN EVERYTHING WHEN EQUITY FALLS THIS LOW.
#
# The exit ladder is per-position. Every rule above asks "is THIS structure
# working", and none of them asks "is the ACCOUNT still solvent". Those are
# different questions, and on 2026-09-04 the difference was visible: equity
# went 17,284 to 13,374 in a session while every individual position was
# behaving within its own rules.
#
# This is the only rule in the file that overrides everything else --
# ORPHAN_ACT_EXPIRY_DAY_ONLY included. A position with a week to run is
# normally left alone precisely because it has time to recover, but that
# argument assumes there is an account left to recover into. When the floor is
# hit, the weeklies are usually where the exposure actually is: that afternoon
# 12,215 of 13,449 in equity sat in two next-Friday SNDK spreads.
#
# IT IS A CIRCUIT BREAKER, NOT A STOP, and the distinction matters. A stop
# asks whether a trade has failed. This asks whether the account can survive
# being wrong again. It will sometimes flatten a book that would have
# recovered -- that is the price of the guarantee, and section 82 measured
# what per-position stops cost when they do the same thing.
#
# 0 disables it, which is the default.
ACCOUNT_FLOOR = float(os.getenv("TRADING_ACCOUNT_FLOOR", "0") or 0)

ORPHAN_TAKE_PROFIT_PCT = float(os.getenv("TRADING_ORPHAN_TAKE_PROFIT", "50"))
ORPHAN_STOP_PCT = float(os.getenv("TRADING_ORPHAN_STOP_PCT", "-25"))
# Credit gets its own, for the reason in the docstring: -25% of a collected
# credit is a few cents and would close everything.
ORPHAN_CREDIT_STOP_PCT = float(os.getenv("TRADING_ORPHAN_CREDIT_STOP_PCT", "-600"))
# REFUSE TO ACT ON A QUOTE THAT IS NOT A QUOTE.
#
# Caught before this ever ran, on the night it was deployed. After the close on
# 2026-09-02 a QQQ 700/710 held for the next session quoted:
#
#     700 call   bid 7.50   ask 12.46   last 9.86    <- 50% of its own value
#     710 call   bid 2.25   ask  2.33
#
# The natural -- bid minus ask, which is what an exit would actually pay --
# came to 5.17 against a 7.49 entry, or -31%, and would have tripped the -25%
# stop on the first cycle of the morning. At sane mids the same spread is worth
# 7.69, or +2.7%. The position was flat and the engine would have dumped it.
#
# A wide book is not a price. Every rule here is a return, a return is only as
# good as the mark, and the mark is only as good as the quote. When either leg
# is quoted wider than this fraction of its own mid, the structure is REPORTED
# and not acted on -- the reading is still logged, so the artefact is visible
# rather than silently skipped.
#
# 0.25 is loose enough for an ordinary 0DTE book late in the day and tight
# enough to reject the case above at 0.50.
ORPHAN_MAX_LEG_SPREAD = float(os.getenv("TRADING_ORPHAN_MAX_LEG_SPREAD", "0.25"))

# ... OR THIS MANY CENTS WIDE, WHICHEVER IS KINDER.
#
# A percentage alone rejects every cheap option. A 0DTE short quoted 0.02/0.03
# is a ONE CENT market -- as tight as an option can be -- and 40% of its own
# mid. The percentage test called that unusable and blocked the close.
#
# The absolute width is what an exit actually pays. Five cents on a
# nearly-worthless leg is a real market; five cents on a 40-dollar leg is
# also fine, and there the percentage never binds anyway. So a quote passes
# if EITHER test passes: tight in cents, or tight in proportion.
ORPHAN_MAX_LEG_SPREAD_ABS = float(os.getenv("TRADING_ORPHAN_MAX_LEG_SPREAD_ABS", "0.05"))

# ACT ON INFERRED PAIRS, or only report them.
#
# Order-based pairing recovers the structure the human actually put on. It
# cannot recover one they LEGGED INTO across separate orders: on 2026-09-03 a
# SNDK 1500 bought on 09-02 and a 1530 sold at 14:14 today are economically a
# spread and appear to the pairer as two unrelated legs.
#
# The fallback pairs leftovers by strike, which is a GUESS -- the same guess
# that made the engine's naked-leg check read a covered book as naked. It is
# usually right and it is not stated fact, so an inferred pair is REPORTED by
# default and acted on only when this is set.
#
# The distinction matters more than it sounds. An inferred entry is the sum of
# two independent fills, which can exceed the structure's own width -- SNDK
# 1500/1530 nets 35.78 against a 30-dollar width, so its BEST possible return
# is -16% and a stop would fire the moment it were managed. Booking that
# automatically, on a pairing nobody stated, is not a decision to take by
# default.
MANAGE_INFERRED = os.getenv("TRADING_ORPHAN_MANAGE_INFERRED", "false").lower() == "true"

# NEVER STOP OUT A POSITION THAT IS ALREADY PROFITABLE AT EXPIRY.
#
# The stop fires on the MARK, and a deep in-the-money spread marks far below
# what it is worth. Observed live 2026-09-03, an hour before the close:
#
#   AVGO 365/355 put, entry 7.88, spot 354.37 -- BELOW the short strike, so
#   the spread holds its full 10.00 of intrinsic. It marked 6.45, a return of
#   -18%, because the short 355 put still carried 3.27 of time premium.
#
# Tomorrow that position becomes 0DTE and the -25% stop goes live at a mark of
# 5.91. One tick and the engine books -190 on a structure worth +212 held to
# expiry. The underlying had moved IN ITS FAVOUR and the rule would have sold
# it for that reason.
#
# So: if INTRINSIC exceeds what was paid, holding to expiry pays more than the
# entry cost and a mark depressed by time value is not a reason to sell. Time
# premium on the short leg goes to zero on its own; that is arithmetic, not a
# forecast. The stop still fires when intrinsic itself has fallen through the
# entry, which is the case it was built for -- a genuine adverse move.
#
# This does NOT disable the stop. It removes the one situation where the stop
# does the opposite of its job.
STOP_RESPECTS_INTRINSIC = os.getenv(
    "TRADING_ORPHAN_STOP_RESPECTS_INTRINSIC", "true").lower() == "true"

# AND THE SAME FOR THE STALL, WHICH IS THE MORE EXPENSIVE HALF.
#
# The stop guard above was built first and stopped there, which was my error:
# the stall has the identical defect and a worse consequence. The stop only
# fires on losers; the stall fires on WINNERS, which is exactly where giving
# up intrinsic costs the most.
#
# It cost 1,250 dollars within the hour. 2026-09-03 17:20:
#
#   ORPHAN STALL_LATER: closing SNDK 1500/1540 x1 -> booked +345
#
# Entry 24.05, sold near 27.50, SNDK at 1558 -- above BOTH strikes, so the
# spread held its full 40.00 of intrinsic and pays +1,595 at expiry. The stall
# read a mark fading from time premium on the short 1540 and called it a
# profit that had stopped climbing.
#
# WITH THIS ON, THE STALL STILL WORKS -- it is measured on INTRINSIC rather
# than on the mark. A genuine reversal (SNDK actually falling back through
# 1540) shrinks intrinsic and books the trade. Time premium bleeding out of a
# short leg does not, because that is not the profit going away, it is the
# profit arriving.
STALL_RESPECTS_INTRINSIC = os.getenv(
    "TRADING_ORPHAN_STALL_RESPECTS_INTRINSIC", "true").lower() == "true"

# A RULE THAT LOCKS IN PROFIT MUST NOT REALISE A LOSS DOING IT.
#
# The stall now DECIDES on intrinsic and still EXECUTES at the mark, and on a
# deep in-the-money spread the mark sits below the entry. So a small intrinsic
# decline -- a real one, correctly detected -- triggers a sale at a price that
# books a loss on a position that was profitable at expiry.
#
# It cost about 263 dollars on 2026-09-03 across three closes:
#
#   AVGO 365/355 -113   intrinsic 9.40 vs 7.88 entry  (+193 at expiry)
#   NVDA 220/230  -15   intrinsic 9.54 vs 8.15 entry  (+139 at expiry)
#   MU   930/950 -135   intrinsic 20.00 vs 12.90 entry (+710 at expiry)
#
# Each fired legitimately by its own logic. Each sold a winner at a loss.
#
# So the stall additionally requires that the exit ACTUALLY BOOKS A GAIN. If
# the mark is below entry there is no profit to protect, and the case is the
# STOP's -- which is already intrinsic-aware and fires when intrinsic itself
# falls through the entry. The two rules then divide cleanly: the stall
# protects gains, the stop limits losses, and neither does the other's job
# badly.
STALL_MUST_BOOK_A_GAIN = os.getenv(
    "TRADING_ORPHAN_STALL_MUST_BOOK_GAIN", "true").lower() == "true"

# AND "A GAIN" HAS TO MEAN SOMETHING. The rule above stops at zero: any
# positive number passes, including eight cents.
#
# 2026-09-16, six stall closes in one session, every one of them legitimate by
# its own logic and none of them worth taking:
#
#     NVDA 212/218 x2   +0.3%   +$2    off a +20.6% peak
#     QQQ  710/712 x3   +3.9%   +$12   off a +46.1% peak
#     INTC 101/103 x5   +2.9%   +$15   off a +46.2% peak
#     INTC  98/102 x5   +2.6%   +$40   off a +32.5% peak
#     SNDK 1530/1550    +6.3%   +$60   off a +95.6% peak
#     SNDK 1520/1550    +1.3%   +$20   off a +83.6% peak
#
# A rule that exists to PROTECT A GAIN, firing to bank 1.5% of the peak it
# armed on, is not protecting anything -- it is closing the position and
# calling the fee a profit.
#
# MEASURED ON THE MARK, deliberately, because the mark is what the exit
# actually realises. The stall DECIDES on intrinsic and that is right -- it is
# what tells a reversal from decay -- but the question here is a different
# one: is the money you would walk away with worth walking away for. Mixing
# the two bases is what produced this whole family of bugs (see
# ORPHAN_MAX_DRAG_WIDTH), so this half is priced where it is paid.
#
# THIS DOES NOT PROTECT THE POSITION, and that is the trade. A winner that
# fades past the floor now runs on to the stop or the flatten instead of
# banking a token amount on the way down. That is the intent: the alternative
# on every one of the six above was worth more than the exit taken.
#
# 0 restores the old behaviour -- any gain, however small.
STALL_MIN_GAIN_PCT = float(
    os.getenv("TRADING_ORPHAN_STALL_MIN_GAIN_PCT", "8") or 0)

# THE UNDERLYING STOP. Close a debit spread that is OUT OF THE MONEY.
#
# WHY IT EXISTS, 2026-09-16. A QQQ 710/714 x6 stopped out for -716 while QQQ
# moved 0.3% and the spread mark moved 130%, then reversed six minutes later
# to a value worth -228. Near expiry the mark is a gamma-amplified, noisy
# rendering of the underlying, and a percentage stop on it is a stop on noise:
# it sat above -35% for twenty minutes while QQQ drifted, gapped 1.17 -> 1.00
# without the underlying doing anything dramatic, and the five-minute
# confirmation then ran while the mark halved again.
#
# intrinsic == 0 on a debit spread says exactly one thing, with no noise in
# it: THE UNDERLYING IS AT OR BEYOND THE LONG STRIKE. Nothing but premium is
# left and only a move in the underlying can bring it back.
#
# WHAT THE MEASUREMENT ACTUALLY SAID, 181 positions over 9 sessions, and it is
# not what was expected:
#
#     LIVE  tgt30 stop-35 cf5              -26,215
#     + OTM 0min, any OTM spread           -25,152   <- the only one that wins
#     + OTM 0min, only if it WAS itm       -26,848
#     + OTM 2min, only if it WAS itm       -26,871
#     + OTM 2min, any                      -28,108
#     + OTM 5min, any                      -27,634
#
# So the rule that earns its place is NOT "it was working and broke" -- that
# version loses to holding. It is the blunter one: DO NOT HOLD AN OUT-OF-THE-
# MONEY DEBIT SPREAD. And it must act immediately; every delayed variant is
# worse than no rule at all, which is the opposite of how the mark stop
# behaves and is the reason this is a separate rule rather than a tuning.
#
# THE EDGE IS SMALL AND CONCENTRATED. +1,063 over 181 positions is about six
# dollars each, and per session it is better on one, identical on four and
# WORSE on three -- including the session that motivated it. Deployed at the
# operator's explicit instruction after that was put to them twice. It is one
# environment variable to undo.
#
# THE RISK IT CARRIES, named because the backtest cannot see it: a spread
# ENTERED out of the money is closed on its next cycle. The entry band
# (30-75% of width) permits such entries, so this can churn one. Watch for an
# OTM_STOP firing within a minute of an entry -- that is this, and it is the
# first thing to check if the rule looks wrong.
# THE TAPE EXIT RESPECTS INTRINSIC, like the stop (2026-09-23, section 218).
#
# MU 1065/1075 x16 at 10:47: MU 1076 under a falling VWAP, intrinsic 10.00
# against a 6.15 entry, marked 5.80 (-5.7%) on short-leg time premium. The
# tape clock was 9 of 15 minutes from selling a structure worth 10.00 at
# expiry for 5.80. The stop already declines exactly this (STOP_RESPECTS_
# INTRINSIC); the tape exit did not, and was also blocked or not depending on
# whether the mark happened to be under the stop level that minute.
#
# NOT MEASURED against section 211's +9,891; operator decision, live. Once
# intrinsic falls through the entry the tape clock runs as before.
TAPE_EXIT_RESPECTS_INTRINSIC = os.getenv(
    "TRADING_ORPHAN_TAPE_EXIT_RESPECTS_INTRINSIC", "true").lower() == "true"

# THE SHORT-STRIKE GUARD (2026-09-23, section 219). A same-day debit spread
# is worth its full width while the underlying is beyond the SHORT strike and
# loses expiry value every point after that. The stop waits for intrinsic to
# fall under the ENTRY (MU 1065/1075 @ 6.15: MU under ~1071) and the tape exit
# now waits the same way, so nothing acted in the 1075 -> 1071 slide.
#
# This closes when the underlying is on the wrong side of the short strike
# (minus BUFFER points) AND under a VWAP moving against the structure, for
# MINUTES continuous minutes. Either condition failing for a minute -- a bounce
# back over the strike, a VWAP that flattens -- resets the clock, so a wiggle
# through the strike does not sell. Sells at market (cancelling the engine's
# own ask first), i.e. still at a time-value discount: a small loss taken to
# avoid a larger one. Operator decision, live; NOT MEASURED -- replay pending.
ORPHAN_STRIKE_GUARD = os.getenv("TRADING_ORPHAN_STRIKE_GUARD", "false").lower() == "true"
ORPHAN_STRIKE_GUARD_MINUTES = float(os.getenv("TRADING_ORPHAN_STRIKE_GUARD_MINUTES", "3") or 3)
ORPHAN_STRIKE_GUARD_BUFFER = float(os.getenv("TRADING_ORPHAN_STRIKE_GUARD_BUFFER", "0") or 0)

# THE UNDERLYING STOP (2026-09-23, section 221). Judge a same-day debit by the
# UNDERLYING's price, not the spread's mark.
#
# MU 1070/1080 x18 @ 5.64 (break-even MU 1075.64): the intrinsic hold-off kept
# the mark stop off at -12% and -18% while MU was above break-even, released at
# -20% when it crossed, and the 2-minute mark confirmation sold at -30%
# (-2,906). Nothing watched MU at the line itself. This does: MU on the wrong
# side of break-even (long strike +/- entry) plus CUSHION points, AND under a
# VWAP moving against it (REQUIRE_TAPE), for MINUTES continuous minutes ->
# close. A bounce resets it -- the operator asked for protection "not in
# haste". Below the short strike the armed strike guard normally acts first;
# this is the backstop for positions that never got that far. NOT MEASURED.
ORPHAN_UNDER_STOP = os.getenv("TRADING_ORPHAN_UNDER_STOP", "false").lower() == "true"
ORPHAN_UNDER_STOP_MINUTES = float(os.getenv("TRADING_ORPHAN_UNDER_STOP_MINUTES", "2") or 2)
ORPHAN_UNDER_STOP_CUSHION = float(os.getenv("TRADING_ORPHAN_UNDER_STOP_CUSHION", "0") or 0)
ORPHAN_UNDER_STOP_REQUIRE_TAPE = os.getenv(
    "TRADING_ORPHAN_UNDER_STOP_REQUIRE_TAPE", "true").lower() == "true"


def under_stop_line(right: str, long_strike: float, entry_abs: float,
                    cushion: float = 0.0) -> float:
    """The underlying price the UNDER stop defends: break-even +/- cushion."""
    if right == "C":
        return long_strike + entry_abs + cushion
    return long_strike - entry_abs - cushion


ORPHAN_OTM_STOP = os.getenv(
    "TRADING_ORPHAN_OTM_STOP", "true").lower() == "true"
ORPHAN_OTM_STOP_MINUTES = float(
    os.getenv("TRADING_ORPHAN_OTM_STOP_MINUTES", "0") or 0)
ORPHAN_OTM_STOP_FLOOR = float(
    os.getenv("TRADING_ORPHAN_OTM_STOP_FLOOR", "0") or 0)

# THE STALL FOR POSITIONS THAT EXPIRE LATER.
#
# The 0DTE stall is deliberately unarmed -- any positive peak starts it --
# because a same-day position has no tomorrow to recover into. A multi-day
# position does, so an unarmed stall would book it at 09:35 on a +2% wiggle
# and forfeit the rest of the week. That is why the stall was 0DTE-only.
#
# But refusing to watch at all gives up the other half: a spread that runs to
# a good profit intraday and rolls over gets carried back down to nothing,
# which is the case this was built for -- a NVDA 215/225 expected to reach 228
# today and not beyond.
#
# So: ARM on a real profit, then protect it with a SMALL giveback. Arming is
# what makes a tight giveback safe here; without it the two settings fight.
# The quiet period is longer than the 0DTE one for the same reason -- a
# multi-day position is allowed to pause without that meaning it is finished.
ORPHAN_LATER_STALL_ARM_PCT = float(os.getenv("TRADING_ORPHAN_LATER_STALL_ARM", "10"))
ORPHAN_LATER_STALL_MINUTES = float(os.getenv("TRADING_ORPHAN_LATER_STALL_MINUTES", "15"))
# GIVE-BACK IN ATR, WHICH MEANS THE SAME THING ON EVERY POSITION.
#
# The percent give-back below is measured against the ENTRY, so the absolute
# trigger moves every time the entry price does. Measured across three SNDK
# positions in two days at a constant setting of 30:
#
#     entry 26.20  ->  7.86 SNDK points  =  1.01x a typical 5-minute bar
#     entry 21.05  ->  6.31 SNDK points  =  0.81x
#     entry 18.00  ->  5.40 SNDK points  =  0.70x
#
# Same number, three different rules. Every roll silently re-tunes the stall,
# and the operator has to re-derive it by hand to know what it now means.
#
# ATR does not move with the entry. 0.10 ATR is a tenth of an average day on
# that underlying whatever the spread cost, so a setting made once holds
# across rolls, across names, and across price levels.
#
# Set above 0 to use it; the percent below stays the fallback.
ORPHAN_LATER_STALL_GIVEBACK_ATR = float(
    os.getenv("TRADING_ORPHAN_LATER_STALL_GIVEBACK_ATR", "0") or 0)

# A GIVE-BACK EXPRESSED AS A SHARE OF THE GAIN, WHICH IS THE ONLY BASIS THAT
# MEANS THE SAME THING ON TWO DIFFERENT POSITIONS.
#
# Anchored to the ENTRY, one setting is several rules. Measured on 2026-09-11:
#
#   QQQ  714/717 x18 @ 2.04   max return  +47%   40 points = 85% of the band
#   SNDK 1670/1730 x2 @ 32.27 max return  +86%   40 points = 47% of the band
#
# Same number, same units, and on the QQQ spread it surrendered $1,469 of a
# $1,728 peak before the trail could act -- while on SNDK the day before it
# booked +$771 from a +89% peak. Nothing about the setting changed; the
# structure did, and an ITM debit spread whose cost is two thirds of its width
# has almost no profit band for a fixed give-back to sit inside.
#
# As a fraction of the PEAK it is self-scaling, which is what the engine's own
# trail has always done (TRADING_TRAIL_GIVEBACK=0.20 is 20% of the peak). 0.30
# gives back a third of the run on any structure at any entry.
#
# Off by default: every give-back measurement on this account was taken on the
# flat percent, and this changes what the number means, not just its value.
ORPHAN_STALL_GIVEBACK_FRACTION = float(
    os.getenv("TRADING_ORPHAN_STALL_GIVEBACK_FRACTION", "0") or 0)

# THE SAME IDEA, DENOMINATED IN THE PROFIT BAND RATHER THAN THE PEAK.
#
# Share-of-peak self-scales, but the peak is an accident of how far a position
# happened to run: the same setting is a different rule on a spread that
# peaked +15% and one that peaked +47%. The BAND -- width minus entry -- is
# fixed by the structure the moment it is opened, which is what makes two
# positions comparable, and it is the unit stall_replay.py measures in, so a
# number picked from a replay can be typed in here unconverted.
#
# 2026-09-11, first fire against holding, replayed on 1-minute bars:
#
#            714/717 @2.04        712/716 @3.20
#   10%       +$594 (22 fires)     +$480          fires on noise
#   20%       +$342                +$480          best on this tape
#   30%       +$126                +$405
#   40%       never                never          too wide to act
#   holding   -$2,142              -$350          QQQ closed 714.85
#
# The flat 40 points in force that afternoon was 85% and 40% of those two
# bands and fired on neither.
ORPHAN_STALL_GIVEBACK_BAND = float(
    os.getenv("TRADING_ORPHAN_STALL_GIVEBACK_BAND", "0") or 0)

# THE SAME BAND FRACTION, BUT FOR POSITIONS THAT DO NOT EXPIRE TODAY.
#
# WHY THIS HAD TO EXIST, 2026-09-16. ORPHAN_STALL_GIVEBACK_BAND was global and
# it SILENTLY ERASED THE 0DTE/WEEKLY SPLIT. _giveback_points tries the band
# before the flat percent, so both callers were overridden and a weekly gave
# back exactly what a same-day position gave back -- while the flat
# TRADING_ORPHAN_LATER_STALL_GIVEBACK=40 sat in .env.production looking as
# though it were in force. It never fired once.
#
# That is the opposite of the reasoning the later-stall knobs were built on,
# stated in their own comment: a multi-day position is ALLOWED TO PAUSE
# without that meaning it is finished. A weekly has days for the underlying to
# come back and its intrinsic moves slowly; the same giveback that reads as a
# reversal on a 0DTE reads as an afternoon on a weekly.
#
# Falls back to the 0DTE band when unset, so an existing deployment behaves
# exactly as before.
#
# NOT MEASURED. exit_backtest cannot settle a weekly -- that needs the EXPIRY
# date's close, which for an open position does not exist yet -- so this is an
# operator judgement about how a multi-day position moves, recorded as one.
_later_band = os.getenv("TRADING_ORPHAN_LATER_STALL_GIVEBACK_BAND")
ORPHAN_LATER_STALL_GIVEBACK_BAND = float(
    _later_band if _later_band is not None else ORPHAN_STALL_GIVEBACK_BAND or 0)

ORPHAN_LATER_STALL_GIVEBACK_PCT = float(
    os.getenv("TRADING_ORPHAN_LATER_STALL_GIVEBACK", "3.3"))

# WHAT A WEEKLY EXIT FORFEITS BY CLOSING AT THE MARK.
#
# THE BUG THIS EXISTS FOR, LIVE 2026-09-16. Two SNDK call debits expiring
# 09-18, both deep in the money, were closed by STALL_LATER for +60 and +20:
#
#     1530/1550  entry  9.50  closed 10.10  intrinsic 16.85   drag  6.75
#     1520/1550  entry 15.40  closed 15.60  intrinsic 25.37   drag  9.77
#
# Eighty dollars booked against roughly 1,378 of intrinsic. Nothing
# malfunctioned: the stall ARMS and FIRES on intrinsic -- peaks of +95.6% and
# +83.6%, given back as SNDK slid 1548 to 1544 -- and then CLOSES at the mark.
# Two different bases for one decision.
#
# On 0DTE the two converge into the bell, which is why this never appeared
# before. On a deep ITM spread with two days left the short leg sits nearer
# the money and carries more time premium, so the mark sits a third of a width
# below intrinsic and that gap IS the position.
#
# STALL_MUST_BOOK_A_GAIN did not catch it because it asks only whether the
# mark beats the entry -- 15.60 against 15.40 is a gain, and passes, while
# 9.77 of intrinsic goes out of the door with it.
#
# So: a later-expiry structure does not close while doing so forfeits more
# than this share of its WIDTH in extrinsic drag. Width, not entry, for the
# reason ORPHAN_STALL_GIVEBACK_BAND gives -- width is fixed by the structure
# at the moment it is opened and means the same thing on two positions, while
# an entry-anchored number is silently re-tuned by every roll.
#
# THE GUARD RELEASES ITSELF. Extrinsic goes to zero at expiry, so the drag
# shrinks as the week runs out and a genuinely finished position becomes
# closeable exactly when closing stops costing anything. It also shrinks when
# BOTH legs go deep ITM, which is the case the target is for.
#
# IT DOES NOT SUPPRESS LOSS PROTECTION. The later stop is gated separately on
# intrinsic_ok, which is false the moment intrinsic falls below the entry --
# a position that stops being profitable-at-expiry re-arms the stop whatever
# this says. The guard declines to bank a token gain; it never declines to cut
# a loss.
#
# 0 disables it and restores the 2026-09-16 behaviour.
#
# SCOPED TO WEEKLIES FIRST, AND THAT WAS WRONG BY FIFTY MINUTES. The first
# version of this carried `not zero_dte`, reasoning that on a same-day expiry
# the mark and intrinsic converge into the bell. True at 15:45. False at
# 11:22, which is when an INTC 98/102 x5 -- entry 3.02, four wide -- was
# closed by the 0DTE STALL for +40.00:
#
#     mark 3.10   intrinsic 3.80   drag 0.70 = 17.5% of width
#     INTC then ran to 102.07, above the short strike, and the spread sat at
#     its maximum intrinsic: +490 held to expiry against the +40 booked.
#
# books_a_gain passed on EIGHT CENTS. Four and a half hours from expiry a
# 0DTE ITM spread carries the same drag a weekly does, because the drag comes
# from the short leg sitting nearer the money, not from the calendar. The
# ceiling belongs to the STRUCTURE, so it applies to every expiry.
#
# The old name is still read so a deployment that set it keeps working.
ORPHAN_MAX_DRAG_WIDTH = float(
    os.getenv("TRADING_ORPHAN_MAX_DRAG_WIDTH",
              os.getenv("TRADING_ORPHAN_LATER_MAX_DRAG_WIDTH", "0.15")) or 0)

# AND THE GUARD HAS TO LET GO WHEN ITS PREMISE EXPIRES.
#
# ORPHAN_MAX_DRAG_WIDTH refuses a profit exit because extrinsic returns at
# expiry, so holding recovers the gap. THAT IS TRUE ONLY WHILE THE POSITION
# IS STILL AT ITS PEAK. Once intrinsic starts falling off the high, holding
# does not recover the drag -- it loses intrinsic as well -- and the guard is
# blocking an exit for a reason that has stopped applying.
#
# THE DEAD ZONE IT CREATED, found live 2026-09-18 on two SNDK 0DTE spreads
# pinned at maximum intrinsic with SNDK at 1657:
#
#     1605/1630  mark 18.40  intrinsic 25.00  drag  6.60 vs a 3.75 ceiling
#     1600/1640  mark 30.10  intrinsic 40.00  drag  9.90 vs a 6.00 ceiling
#
# If SNDK fell back through the short strikes, THREE rules stood down at once:
# TARGET and STALL on the drag ceiling, and the hard stop on the intrinsic
# guard (intrinsic still exceeded entry, so it "pays at expiry"). The soft
# stop needed the mark down at 9.54. Between SNDK 1630 and 1616 the big
# spread could give back 3,620 dollars with nothing acting at all.
#
# And drag WORSENS as spot falls toward the short strike, because that is
# where the short leg carries the most premium -- so the guard stays shut
# precisely while the giveback happens.
#
# MEASURED, 203 positions over 10 sessions, against the ladder as deployed:
#
#     guard as-is                      +2,850
#     release at -5%  of width         +4,013
#     release at -10% of width         +4,284   <- deployed
#     release at -20% of width         +3,250
#     guard only while pinned at max     +396
#     no guard at all                  -8,633
#
# BE HONEST ABOUT THE SHAPE OF THAT: the whole +1,434 comes from ONE session
# (09-14, +1,694). Two sessions lose 80 and 614, six are identical. It is
# shipped because the DEFECT is logical rather than statistical -- a premise
# that has expired -- and because the asymmetry is favourable. If it starts
# costing money, this is the first thing to turn off.
#
# 0 disables the release and restores the unconditional guard.
ORPHAN_DRAG_RELEASE_WIDTH = float(
    os.getenv("TRADING_ORPHAN_DRAG_RELEASE_WIDTH", "0.10") or 0)

# WHICH SERIES THE LATER TARGET READS.
#
# It was measured on the mark, and on the same two SNDK spreads that made it
# unreachable: 0.90 of width is 27.00 and 18.00, against marks of 15.60 and
# 10.10 -- while intrinsic was ALREADY 25.37 and 16.85. A target the mark
# reaches only as extrinsic dies is a target that fires at expiry, by which
# point it has nothing left to do.
#
# Intrinsic is what the structure is worth if held, which is the question the
# target is actually asking. Paired with the drag guard above: the target says
# the position is finished, the guard says whether closing it realises that.
LATER_TARGET_ON_INTRINSIC = os.getenv(
    "TRADING_ORPHAN_LATER_TARGET_ON_INTRINSIC", "true").lower() == "true"

# THE STALL, WITH ITS OWN GIVEBACK.
#
# TRADING_STALL_GIVEBACK_PCT is shared by the morning ride, the afternoon
# credit trade and this module, and five points means something different in
# each. On the engine's morning debit spread it is a fraction of a premium
# priced near the money. On a credit spread it is five points of the credit
# COLLECTED, which can be a couple of cents. On a manual deep-ITM spread --
# a QQQ 700/710 bought at 7.49, $10 wide -- five points is 37 cents of value,
# which a position drifting with the underlying gives back without the thesis
# changing at all.
#
# One number cannot serve three structures. These default to the shared values
# so nothing moves until they are set deliberately.
# HOW LONG A PEAK MUST SIT QUIET BEFORE A GIVEBACK COUNTS AS A STALL.
#
# 5, AND IT WAS BRIEFLY 2 ON 2026-09-15 BEFORE THE DATA SAID OTHERWISE.
#
# The argument for 2 was real: a QQQ 705/702 peaked +31.0% two minutes before
# the 15:45 flatten and the stall could not arm, so it closed at +8.3%. Peaks
# that day lasted 1-4 minutes and the sub-minute spike never appeared on
# per-minute closes at all.
#
# REPLAYING EVERY QQQ POSITION OF THAT SESSION AGAINST THE ENGINE'S OWN LOGGED
# MARKS SAID THE OPPOSITE. Six positions, exits simulated on the marks the
# engine actually saw:
#
#     tgt30 stop-35 cf5 stall 5min    -152
#     tgt30 stop-35 cf5 stall 2min    -210
#
# The difference is one position, and it is not luck. QQQ 705/703 peaked +9.4%,
# fell to +1.9%, sat flat for six minutes, then ran to the +30% target:
#
#     18:42:52   +1.9%   peak +9.4%, 2.1 min ago   <- a 2-min stall books here
#     18:43-45   +1.9% to +7.5%   flat
#     18:48:09  +30.2%   TARGET, +165 booked
#
# Two minutes books that at +1.9%. Five lets it recover. THE WAITING IS THE
# POINT: a giveback rule that fires while a position is merely resting turns
# every pause into an exit, and pauses are what winners do.
#
# The 705/702 case that prompted the change is still real -- a peak two minutes
# before a flatten cannot be caught by any stall with a longer window. That is
# an argument about the FLATTEN boundary, not about the stall, and it should
# not be fixed by making the stall fire on rests.
STALL_MINUTES = float(os.getenv("TRADING_ORPHAN_STALL_MINUTES",
                                os.getenv("TRADING_STALL_MINUTES", "0")))
STALL_GIVEBACK_PCT = float(os.getenv("TRADING_ORPHAN_STALL_GIVEBACK_PCT",
                                     os.getenv("TRADING_STALL_GIVEBACK_PCT", "0")))

# Peak tracking must survive a container recreate or the stall resets on every
# deploy and can never fire. A file under the mounted working directory.
# CEILING: book when there is no meaningful upside left to hold for.
#
# The engine has this for its own ride (RIDE_CEILING_FRACTION) and orphans did
# not. It matters more here than there, because of a shape the stall cannot
# handle: as a debit spread approaches its full width it stops MOVING, and a
# position that is not moving cannot give back enough to trip a stall. The
# protection weakens exactly as the remaining upside disappears.
#
# A QQQ 700/710 bought at 7.49 can be worth at most 10.00, so 90% of maximum
# return is a value of about 9.75. Holding past that risks the whole width for
# the last 25 cents.
#
# For a credit structure the maximum is the credit itself -- the spread decays
# to zero and you keep all of it -- so the ceiling is 90% of the credit.
ORPHAN_CEILING_FRACTION = float(os.getenv("TRADING_ORPHAN_CEILING", "0.90"))

# A TARGET EXPRESSED AS RETURN ON COST, which the ceiling above is not.
#
# The ceiling is a fraction of MAX profit -- entry + f x (width - entry) -- so
# it is bounded by the width and lands near 95% of it on a position bought at
# 60-80%. It has never fired: 0 of 44 structures reached it.
#
# This is the other shape: sell when the mark reaches entry x (1 + r/100),
# regardless of width. On a spread bought at half its width the two coincide;
# on a deep one they diverge sharply, and this one can be unreachable where
# the ceiling is merely distant.
#
# MEASURED AT +50%, and it does not pay on the sessions in hand:
#
#     target   fires  helps      total   vs off
#     +30%         5      0     +26731    -4748
#     +50%         2      0     +30179    -1300
#     +75%         1      0     +31439      -40
#     +100%        0      0     +31479       +0
#
# The two +50% fires: SNDK 1500/1560 booked +1,656 against +2,816 held, and
# QQQ 722/719 +190 against +370. Zero helps at every level, which is the same
# result every profit target on this book has produced (section 88).
#
# DEPLOYED AT 50 ANYWAY, deliberately and at the account owner's direction,
# to watch it forward on a book whose entries are moving toward half the
# width -- where the reachability argument is different from the 60-80%
# entries these numbers are drawn from. Recorded here as deployed AGAINST the
# measurement rather than on the strength of it.
ORPHAN_TARGET_RETURN_PCT = float(
    os.getenv("TRADING_ORPHAN_TARGET_RETURN_PCT", "0") or 0)

# FORCE CLOSE. The engine flattens its own book at 15:45; orphans had no time
# exit at all and would have ridden into expiry.
#
# These are physically settled. A spread left to expire is not a cash
# settlement, it is an exercise and an assignment in shares -- and if the
# underlying finishes between the strikes it is the pin case. On 2026-09-02
# the operator deliberately flattened before the 16:00 settlement print for
# exactly that reason, and leaving orphans to expire contradicts it.
ORPHAN_FORCE_CLOSE = os.getenv("TRADING_ORPHAN_FORCE_CLOSE", "15:45").strip()

# NO LOSS-TAKING BEFORE THIS TIME. Empty disables the gate, which is the
# default and leaves behaviour unchanged.
#
# THE OPEN IS NOT A SIGNAL, IT IS A SPREAD. For the first stretch of the
# session the book is at its widest and the mark at its least trustworthy, so
# a structure can print a "peak" on one wide quote and a giveback on the next,
# or a -25% mark on a spread that has not moved, with nothing having happened
# to the underlying. On a Friday expiry that dips early and comes back, both
# rules fire on microstructure and book a position that had hours to work.
#
# GATED -- the loss side, and the early-profit side:
#
#   the stop     a -25% mark at 09:31 is as likely to be a wide quote as a
#                real move.
#   the stall    books a WINNER early on a judgment about a mark, which is the
#                judgment the open is worst at making.
#
# LIVE FROM THE FIRST CYCLE:
#
#   the ceiling  fires at 90% of maximum. Taking that at 09:35 is a good
#                outcome, not a premature one, and it is a decision about a
#                position that has already won.
#   force close  is about assignment, not value. 15:45 regardless.
#
# THIS TRADES TAIL PROTECTION FOR NOISE IMMUNITY, KNOWINGLY. These are DEBIT
# spreads, so loss is bounded by the premium paid rather than by the stop: the
# gate accepts up to the full debit on a genuine gap in exchange for not
# booking losses into an opening spread that reverses. On 0DTE a 2% adverse
# move frequently does not come back, and nothing here pretends otherwise.
# WHAT IT COVERS, stated because the answer was got wrong once. It gates every
# rule that makes a JUDGEMENT ABOUT VALUE off a mark that the opening spread
# has not settled yet:
#
#   the 0DTE stop, the slow stop, the later stop, the OTM stop
#   both stalls, the intrinsic giveback
#   TARGET, LATER_TARGET and the ceiling      <- added 2026-09-17
#
# NOT the force close, which is about assignment and where time beats
# everything, and not the account floor, which has already decided the account
# is not working and outranks any judgement about one position.
#
# It previously covered six of twelve branches and NO take-profit at all, so
# "start watching at 09:35" delayed the loss side and left the profit side
# firing into the bell. A SNDK weekly sold on TARGET at 09:31 (section 174).
ORPHAN_HOLD_UNTIL = os.getenv("TRADING_ORPHAN_HOLD_UNTIL", "").strip()

# THE WEEKLY BOOK HAS ITS OWN OPENING QUIET PERIOD. 09:30 was chosen for
# PINNED 0DTE positions, where the first print can be the whole day's profit
# and a rule that waits misses it. A position with days to run has nothing to
# book at the open and a great deal to lose to the opening spread, so it waits
# longer. Empty means "same as ORPHAN_HOLD_UNTIL", which is how every
# deployment before 2026-09-18 behaved.
ORPHAN_LATER_HOLD_UNTIL = os.getenv("TRADING_ORPHAN_LATER_HOLD_UNTIL", "").strip()

# ASK MODE: REST A SELL ABOVE THE BID ON A PINNED SPREAD, AND WALK IT DOWN.
#
# Measured 2026-09-19 over 46 positions / 9 sessions: a debit spread sitting
# at FULL intrinsic was bid a median 59% of its width in the first half hour,
# 72-79% through midday, and 90%+ in one cycle in five between 12:00 and
# 14:00 -- never 93%. Selling at the mark the moment a target is met takes
# the discount; holding to the flatten takes the pin risk. This is the third
# option: ask for a price the market has actually paid, and step toward the
# bid only while the underlying is weakening (below its session VWAP).
#
# WHY IT LIVES HERE AND NOT IN A SCRIPT. A working order on a structure's
# legs makes the ladder stand down (in_flight) -- correctly, it must not sell
# a position twice. A resting limit placed from OUTSIDE therefore switches off
# the stop, the stall and the flatten for as long as it rests, which is the
# failure the in_flight comment records. Placed from inside, the engine knows
# the order is its own, keeps every loss rule live, and cancels the ask first
# whenever one of them needs to act.
#
# The floor is the TARGET level (entry * (1 + TARGET_RETURN_PCT)), so the ask
# can never end up asking LESS than the rule it replaces. START_WIDTH is a
# fraction of the width; 0.88 is just above the best first-half-hour bid in
# the sample (86%). TARGET and CEILING are suppressed while an ask rests --
# the ask IS that exit, asking more; STALL, GIVEBACK and every loss rule are
# not suppressed, they cancel the ask and act. Off unless TRADING_ORPHAN_ASK
# is true. 0DTE only by default: a weekly's extrinsic is days away from zero.
ORPHAN_ASK = os.getenv("TRADING_ORPHAN_ASK", "false").lower() == "true"
ORPHAN_ASK_START_WIDTH = float(os.getenv("TRADING_ORPHAN_ASK_START_WIDTH", "0.88") or 0)
ORPHAN_ASK_FLOOR_WIDTH = float(os.getenv("TRADING_ORPHAN_ASK_FLOOR_WIDTH", "0") or 0)
ORPHAN_ASK_STEP = float(os.getenv("TRADING_ORPHAN_ASK_STEP", "0.10") or 0.10)
ORPHAN_ASK_STEP_MINUTES = float(os.getenv("TRADING_ORPHAN_ASK_STEP_MINUTES", "3") or 3)
ORPHAN_ASK_VWAP_FROM = os.getenv("TRADING_ORPHAN_ASK_VWAP_FROM", "09:40").strip()
ORPHAN_ASK_CANCEL_BY = os.getenv("TRADING_ORPHAN_ASK_CANCEL_BY", "15:40").strip()
ORPHAN_ASK_ZERO_DTE_ONLY = os.getenv("TRADING_ORPHAN_ASK_ZERO_DTE_ONLY", "true").lower() == "true"
# Level-based profit exits the ask replaces while it rests. Everything else
# -- stalls, give-back, every stop, the flatten -- cancels the ask and acts.
_ASK_SUPPRESSED = {"TARGET", "CEILING", "LATER_TARGET"}

# THE SLOW STOP: A LEVEL THAT HAS TO HOLD, NOT A LEVEL THAT IS TOUCHED.
#
# ORPHAN_STOP_PCT is a FAST stop -- it fires on the first cycle through its
# level, which is what a gap needs. It is set loose (-40%) because anything
# tighter fires on the extrinsic swing an in-the-money spread carries all day
# (section 86). That leaves a hole: a position that erodes steadily without
# ever reaching -40% is never caught at all.
#
# This is the other half. A shallower level that must hold CONTINUOUSLY, with
# the clock reset the instant the mark recovers above it. Sustained weakness
# is a different signal from a touch: a spread that sits 20% underwater for
# half an hour has had every chance to come back and has not.
#
# MEASURED over 39 structure-days, hold-to-expiry = +27,195:
#
#     fast    slow            fires helps      P&L    vs hold
#     -40%    none                4     3   +28735     +1540
#     none    -20% / 30min        3     3   +28636     +1441
#     -40%    -20% / 30min        5     4   +29133     +1938   <- deployed
#     -10%    instantaneous      23     6   +14636    -12559
#
# The last row is the same idea WITHOUT the duration test, and it is the worst
# rule measured in this file. The duration filter is the whole difference:
# -10% touched costs 12,559, -10% held for an hour gains 996.
#
# IT DELIBERATELY DOES NOT RESPECT THE INTRINSIC GUARD, and that is the one
# uncomfortable part. With STOP_RESPECTS_INTRINSIC applied it never fires at
# all -- every measured configuration collapses back to the fast stop alone.
# Its entire value comes from closing a position that still pays at expiry on
# paper but has been grinding for half an hour, which is exactly the case the
# guard was written to protect. Both cannot be right, and on this evidence the
# grind is real: STX 850/840 exited at -180 against -578 held.
#
# THAT EVIDENCE IS ONE TRADE. Five fires, four helped, one of them SLOW.
ORPHAN_SLOW_STOP_PCT = float(os.getenv("TRADING_ORPHAN_SLOW_STOP_PCT", "0") or 0)

# A STOP FOR A POSITION THAT IS NOT EXPIRING TODAY.
#
# Both stops above are gated on zero_dte, so until now a weekly had none at
# any setting -- TRADING_ORPHAN_STOP_PCT=-10 did nothing to a position until
# the Friday it expired. That was deliberate (section 88 measured stops as a
# tax on multi-day positions, and a move that is terminal on expiry day is
# survivable with five sessions left), and it cannot be switched on with the
# existing flags either: TRADING_ORPHAN_TODAY_ONLY=false would give a weekly
# the stop AND the 15:45 flatten, closing a five-day position on day one.
# The two are welded to one flag, so this is its own.
#
# THREE GUARDS, because a stop with days to run is the easiest rule to get
# wrong and the most expensive:
#
#   DEBITS ONLY. -10% on a credit structure is absurd -- the return is
#   measured against the credit collected, and a credit spread routinely
#   trades -100% intraday and expires worthless anyway. Credit keeps
#   ORPHAN_CREDIT_STOP_PCT, which is -600 and off, exactly as on expiry day.
#
#   INTRINSIC STILL WINS. Section 88's case was a SNDK 1600/1700 at full
#   intrinsic (+3,825 at expiry) marking -0.7% a week out. A stop that sells
#   that is not managing risk, it is paying the spread to exit a winner.
#
#   AND IT MUST PERSIST. A weekly's quote is wider than a 0DTE's and has no
#   convergence pressure, so a single print can show -12% and mean nothing.
#   The 0DTE stop confirms in 0 minutes because there the clock is the risk;
#   here there is no clock, so the confirmation is free.
#
# 0 disables it, which is the default: nothing measured on this account yet
# says a multi-day stop earns its keep.
ORPHAN_LATER_STOP_PCT = float(
    os.getenv("TRADING_ORPHAN_LATER_STOP_PCT", "0") or 0)
ORPHAN_LATER_STOP_MINUTES = float(
    os.getenv("TRADING_ORPHAN_LATER_STOP_MINUTES", "15"))

# THE LATER LADDER SCALES WITH THE SESSIONS LEFT -- section 199.
#
# Until 2026-09-19 every position not expiring today got the same numbers,
# whether it had five sessions left or one. A -45% stop and a +25% stall arm
# are set for a spread with a week to recover; on Thursday afternoon the same
# spread has one session, the least time to come back from -45% and the most
# to lose from a give-back, and the ladder treated it like Monday.
#
# So each LATER number is now interpolated between a ONE-SESSION anchor (the
# 0DTE ladder's values, or close to them) and the FULL-WEEK value already set,
# on f = (sessions_left - 1) / (SCALE_DAYS - 1), clamped to [0, 1]:
#
#     sessions left    1        2        3        4        5+
#     stop            -30%    -33.8%   -37.5%   -41.3%   -45%
#     stop confirm      5       7.5      10       12.5     15 min
#     stall arms at    +5%     +10%     +15%     +20%     +25%
#     stall quiet      10       15       20       25       30 min
#     give-back ATR    0.10     0.14     0.18     0.21     0.25
#
# Sessions are TRADING days between the New York date and the expiry, so a
# Friday weekly bought Monday runs 4, 3, 2, 1 and then the 0DTE ladder on
# Friday itself. A stated assumption, like the rest of the weekly ladder; the
# harness cannot settle a weekly to measure it. TRADING_ORPHAN_LATER_SCALE=false
# restores the flat numbers.
ORPHAN_LATER_SCALE = os.getenv("TRADING_ORPHAN_LATER_SCALE", "true").lower() == "true"
ORPHAN_LATER_SCALE_DAYS = max(2, int(os.getenv("TRADING_ORPHAN_LATER_SCALE_DAYS", "5")))
ORPHAN_LATER_STOP_PCT_1D = float(os.getenv("TRADING_ORPHAN_LATER_STOP_PCT_1D", "-30"))
ORPHAN_LATER_STOP_MINUTES_1D = float(os.getenv("TRADING_ORPHAN_LATER_STOP_MINUTES_1D", "5"))
ORPHAN_LATER_STALL_ARM_1D = float(os.getenv("TRADING_ORPHAN_LATER_STALL_ARM_1D", "5"))
ORPHAN_LATER_STALL_MINUTES_1D = float(os.getenv("TRADING_ORPHAN_LATER_STALL_MINUTES_1D", "10"))
ORPHAN_LATER_STALL_GIVEBACK_ATR_1D = float(os.getenv("TRADING_ORPHAN_LATER_STALL_GIVEBACK_ATR_1D", "0.10"))


def _sessions_to_expiry(st: dict, today=None) -> int:
    """Trading sessions from today (New York) to the structure's expiry, >= 0."""
    try:
        from zoneinfo import ZoneInfo
        exp = datetime.strptime(str(st.get("expiry")), "%y%m%d").date()
        d = today or datetime.now(ZoneInfo("America/New_York")).date()
        try:
            from .market_calendar import is_trading_day
        except Exception:
            is_trading_day = lambda x: x.weekday() < 5     # noqa: E731
        n = 0
        while d < exp:
            d = d.fromordinal(d.toordinal() + 1)
            if is_trading_day(d):
                n += 1
        return n
    except Exception:
        return ORPHAN_LATER_SCALE_DAYS


def later_params(st: dict, today=None) -> dict:
    """The LATER ladder's numbers for THIS structure, scaled by sessions left."""
    full = {"stop_pct": ORPHAN_LATER_STOP_PCT, "stop_minutes": ORPHAN_LATER_STOP_MINUTES,
            "stall_arm": ORPHAN_LATER_STALL_ARM_PCT, "stall_minutes": ORPHAN_LATER_STALL_MINUTES,
            "giveback_atr": ORPHAN_LATER_STALL_GIVEBACK_ATR}
    dte = _sessions_to_expiry(st, today)
    if not ORPHAN_LATER_SCALE or ORPHAN_LATER_STOP_PCT >= 0:
        return dict(full, dte=dte, f=1.0)
    f = min(1.0, max(0.0, (dte - 1) / float(ORPHAN_LATER_SCALE_DAYS - 1)))
    one = {"stop_pct": ORPHAN_LATER_STOP_PCT_1D, "stop_minutes": ORPHAN_LATER_STOP_MINUTES_1D,
           "stall_arm": ORPHAN_LATER_STALL_ARM_1D, "stall_minutes": ORPHAN_LATER_STALL_MINUTES_1D,
           "giveback_atr": ORPHAN_LATER_STALL_GIVEBACK_ATR_1D}
    out = {k: round(one[k] + f * (full[k] - one[k]), 4) for k in full}
    # The ATR give-back only applies when the full-week configuration uses it.
    if ORPHAN_LATER_STALL_GIVEBACK_ATR <= 0:
        out["giveback_atr"] = 0.0
    out.update(dte=dte, f=round(f, 3))
    return out
ORPHAN_SLOW_STOP_MINUTES = float(
    os.getenv("TRADING_ORPHAN_SLOW_STOP_MINUTES", "30") or 30)

# AND THE FAST STOP GETS A SHORT ONE TOO.
#
# It fired on the FIRST cycle through its level, so a single bad print or one
# wide quote was enough to close a position. That is a real failure mode on
# this chain -- section 73 is a whole section about quotes that are not
# markets -- and the quote guard only catches the extreme cases.
#
# Two minutes, which on a one-minute cron is two consecutive confirmations:
#
#     fast          slow           fires helps      P&L    vs hold  mix
#     -40% /  0min  -20% / 30min       5     4   +34335    +1943   FAST 4, SLOW 1
#     -40% /  2min  -20% / 30min       4     4   +34493    +2101   FAST 2, SLOW 2
#     -40% /  5min  -20% / 30min       3     3   +34013    +1621   FAST 1, SLOW 2
#     -40% / 10min  -20% / 30min       3     3   +33833    +1441   SLOW 3
#
# Two minutes filters one bad fire and takes the hit rate to 4 of 4. Longer
# degrades fast: by ten minutes the fast stop never fires at all and the slow
# stop is doing all the work, which is a different rule wearing this one's
# name.
#
# The clock resets when the mark recovers AND when the intrinsic guard stands
# the stop down, so a position that dips through the level and comes back
# starts over rather than accumulating.
# DEFAULT 2, NOT 0. The measurement above says 2 and the code shipped with 0 --
# the feature was built, measured positive, and then deployed switched off, in
# the code default AND in .env.production.
#
# WHAT THAT COST, LIVE, 2026-09-15. A QQQ 708/703 put debit, entry 2.46, peaked
# +15.7% and then printed -10.8% for ONE CYCLE on a five-minute wick:
#
#     12:50   QQQ 705.89   spread -14%    the wick
#     12:53   STOP_LOSS fired, booked -154 across two fills
#     12:55   QQQ 705.45   spread  +4%
#     13:00   QQQ 704.78   spread +31%    past the +30% target
#     13:20   QQQ 704.57   spread +39%
#
# It would have hit its target seven minutes after being closed. One print, no
# persistence, and the position was gone.
#
# A CAVEAT THE NUMBERS ABOVE DO NOT COVER: that sweep ran a -40% fast stop and
# production runs -10%. A stop four times tighter fires on more noise, which
# argues the confirmation matters MORE here, not less -- but it also means two
# minutes of a genuine breakdown costs more at -10% than it did at -40%.
# Yesterday's QQQ 710/714 went -7.8% to -16.5% in two minutes, so this is not
# free. It is the right trade on the evidence available; it is not a free win.
#
# The clock resets when the mark recovers and when the intrinsic guard stands
# the stop down, so only SUSTAINED weakness closes a position.
# DEPLOYED AT 5, WHICH IS NOT WHAT THE SWEEP ABOVE SAYS. That sweep ran a -40%
# fast stop and found 2 minutes best (+2101) with 5 minutes worse (+1621).
# Production runs -10%, which is four times tighter, and at -10% the stop sits
# a QUARTER POINT of QQQ from entry -- 4% of a typical day's range, inside the
# tick noise. A confirmation window tuned for a stop four times wider does not
# transfer, and the operator's judgement after watching it fire on wicks is the
# better evidence available for THIS stop level.
#
# WHAT 5 MINUTES COSTS, and it is not free. The loss is not capped at the stop
# level: during the wait the mark keeps moving, so an exit can land well below
# -10%. There is no slow stop behind it either (ORPHAN_SLOW_STOP_PCT=0), so on
# a genuine breakdown the next rule down is the 15:45 flatten.
#
# THE LEVEL IS STILL THE REAL ISSUE. Section 82 swept the stop itself on 39
# structure-days and found -40% and -60% the only settings that beat holding,
# with the WORST single loss identical (-1,932) at every setting -- tightening
# buys nothing on the bad day and closes the good ones early. -10% is tighter
# than anything that measured positive. Confirmation is a patch on a level that
# wants loosening; if the stop ever moves to -40%, revisit this back to 2.
ORPHAN_STOP_CONFIRM_MINUTES = float(
    os.getenv("TRADING_ORPHAN_STOP_CONFIRM_MINUTES", "2") or 0)

# THE TAPE EXIT: SELL A LOSING 0DTE DEBIT SOONER WHEN THE UNDERLYING IS ON THE
# WRONG SIDE OF A VWAP THAT IS MOVING AGAINST IT.  Section 211, 2026-09-21.
#
# Section 208 asked the opposite question -- should the stop WAIT for the tape
# to agree -- and the answer was no, by 9,260 over nine sessions. This is the
# operator's question the same afternoon: MU's session VWAP fell all morning
# with the price under it while the morning's call debits bled to the stop;
# should that have been the exit? Replayed on the same 130 structure-days, on
# top of the -10%/2min stop, with the tape read on Tradier's 5-minute bars:
#
#     rule                     fires  helps hurts    total    vs stop   worst day
#     stop only (deployed)        54     37    17   +1,609          -    -29,818
#     tape 15 min, losers only    61     41    20  +11,500     +9,891    -25,825
#     tape 15 min, any            79     47    32   +8,087     +6,477    -26,857
#     tape  5 min, losers only    73     43    30   +4,899     +3,289    -28,141
#     tape 30 min, losers only    58     38    20   +5,759     +4,150    -26,334
#
# Both frames, for the first exit rule measured today: 09-09, 09-10 and 09-16
# each 4,000-5,000 better, the two big winning days within a few hundred.
# Fifteen minutes because five fires on noise and thirty arrives too late;
# LOSERS ONLY because selling a winner on the tape gives most of it back,
# which is the lesson the stall already paid for (STALL_MUST_BOOK_A_GAIN).
#
# "Wrong side, moving against" is BOTH: for a call debit the underlying under
# the session VWAP AND the VWAP lower than it was SLOPE_BARS bars ago; for a
# put debit over it AND rising. Either alone is a level or a drift; together
# they say the price the day's volume is paying is walking away from the
# structure. Credit structures are not touched. A read that fails resets the
# clock rather than counting toward it: a rule that sells needs a reading.
#
# It sits BELOW the fast stop in the ladder and above the slow stop, and it
# names the exit TAPE_EXIT so the history can be scored against the rest.
# Off by default; the deployment turns it on.
ORPHAN_TAPE_EXIT = os.getenv("TRADING_ORPHAN_TAPE_EXIT", "false").lower() == "true"
ORPHAN_TAPE_EXIT_MINUTES = float(os.getenv("TRADING_ORPHAN_TAPE_EXIT_MINUTES", "15") or 15)
ORPHAN_TAPE_EXIT_SLOPE_BARS = int(os.getenv("TRADING_ORPHAN_TAPE_EXIT_SLOPE_BARS", "6") or 6)
ORPHAN_TAPE_EXIT_LOSERS_ONLY = (
    os.getenv("TRADING_ORPHAN_TAPE_EXIT_LOSERS_ONLY", "true").lower() == "true")

# THE BAND WHERE NOTHING FIRES, IN DOLLARS OF INTRINSIC GIVEN BACK.
#
# Two guards can be correct individually and silent together:
#
#   the stall  stands down while the mark is under the entry
#              (STALL_MUST_BOOK_A_GAIN -- it will not sell at a loss, which is
#              the rule that saved 845 dollars on MU the previous day)
#   the stop   stands down while intrinsic still exceeds the entry
#              (STOP_RESPECTS_INTRINSIC -- it will not sell something that
#              pays at expiry)
#
# An in-the-money spread lives in BOTH conditions at once: its mark sits below
# entry because the short leg still holds time premium, while its intrinsic
# sits above entry because it is deep in the money. Between those two lines a
# position can shed its entire edge with nothing acting.
#
# MEASURED LIVE, 2026-09-04. MU 995/1005 x5 held maximum intrinsic at 13:00
# and drained for five straight minutes:
#
#     13:00  MU 1005.10  intrinsic 10.00   worth +1,640 at expiry
#     13:05  MU 1002.39  intrinsic  7.39   worth   +385 at expiry
#
# The engine logged "holding" on every one of those cycles, correctly by its
# own rules. 1,255 dollars of EXPIRY value went, and the only reason it was
# seen at all is that a watcher was printing the band.
#
# WHAT THIS RULE IS. A stop measured on the value at EXPIRY rather than on the
# mark. It fires when intrinsic falls this many dollars from its own peak on a
# structure whose peak was genuinely above entry -- a winner that is turning.
# It deliberately does NOT require booking a gain: the whole point is that the
# mark is underwater and holding is getting worse, so it accepts a small loss
# now against a larger one later.
#
# AS A PERCENT OF THE STRUCTURE'S WIDTH, NOT AS DOLLARS.
#
# This was first written as an absolute dollar figure and that cannot work
# across a book. Measured on the live positions of 2026-09-04, a 0.50 dollar
# threshold means:
#
#     SNDK 1690/1720   30 wide    1.7% of width   fires on noise
#     MU   990/1000    10 wide    5.0%
#     QQQ  716/719      3 wide   16.7% of width   barely ever fires
#
# SNDK moves more than 0.50 of intrinsic between two cycles, so the same
# number is a hair trigger on one position and inert on another. The same
# mistake as section 78's percent-with-a-moving-baseline and section 73's
# 25%-of-mid on a 3-cent option: a threshold whose units stop meaning what
# they meant when the context changes.
#
# Width is the natural scale -- it is the full range the intrinsic can travel,
# so a percent of it means the same thing on every structure. 10% gives 3.00
# on the 30-wide SNDK and 0.30 on the 3-wide QQQ, which is the intent.
#
# OFF BY DEFAULT (0). It is a real exit rule, not a correctness fix, and it
# has not been through the harness. Turning it on is a trading decision.
ORPHAN_INTRINSIC_GIVEBACK_PCT = float(
    os.getenv("TRADING_ORPHAN_INTRINSIC_GIVEBACK_PCT", "0") or 0)

# AND IT MUST STILL BE TRUE THIS MANY MINUTES LATER.
#
# Without this the rule fires on the first cycle the threshold is crossed,
# which means it fires on noise. Measured on MU 990/1000 the afternoon it was
# first enabled -- intrinsic through the twenty minutes before it fired:
#
#     17:26  10.00   17:33  10.00   17:39  10.00
#     17:29   9.22   17:34   9.65   17:40  10.00
#     17:30   9.10   17:35   9.96   17:41   9.54
#     17:31   9.62   17:36  10.00   17:42   8.55
#     17:32   9.70   17:37  10.00   17:43   7.89  <- fired
#
# The position breathed 8.68 to 10.00 all afternoon -- routine swings of 1.32
# against a threshold of 1.50. The dip that triggered it lasted THREE MINUTES
# and MU was back at full intrinsic within the hour. It sold at 5.90 into a
# position now worth 10.00, for -390 against +1,660 held: a 2,050 dollar
# mistake made by a rule that could not tell a wobble from a reversal.
#
# A threshold alone cannot tell them apart, because the size of a wobble is a
# property of the underlying, not of the structure. TIME can: a reversal is
# still there five minutes later and a wobble is not. So the giveback must be
# continuously true for this long before it acts, and any new intrinsic high
# clears the clock.
#
# This is the same shape as STALL_MINUTES, and for the same reason -- section
# 43 arrived at a quiet timer on the morning ride by exactly this route.
ORPHAN_GIVEBACK_CONFIRM_MIN = float(
    os.getenv("TRADING_ORPHAN_GIVEBACK_CONFIRM_MIN", "5") or 0)

STATE_PATH = os.getenv("TRADING_ORPHAN_STATE", "orphan_peaks.json")


def _load() -> dict:
    """State file: {"peaks": {...}, "structures": {...}}.

    A legacy flat file is read as peaks-only, so an upgrade does not lose the
    ratchet on a position that is already open.
    """
    try:
        with open(STATE_PATH) as fh:
            raw = json.load(fh)
    except Exception:
        return {"peaks": {}, "structures": {}}
    if not isinstance(raw, dict):
        return {"peaks": {}, "structures": {}}
    if "peaks" in raw or "structures" in raw:
        return {"peaks": raw.get("peaks") or {}, "structures": raw.get("structures") or {}}
    return {"peaks": raw, "structures": {}}


def _save(state: dict) -> None:
    try:
        with open(STATE_PATH, "w") as fh:
            json.dump(state, fh)
    except Exception:
        logger.exception("Could not persist orphan state — the stall will restart cold.")


def _parse(symbol: str) -> "tuple | None":
    """(root, expiry, right, strike) from an OCC symbol, or None."""
    try:
        root = tradier_orders.occ_root(symbol)
        tail = symbol[len(root):]
        if len(tail) != 15:
            return None
        return root, tail[:6], tail[6], int(tail[7:]) / 1000.0
    except Exception:
        return None


_ATR_CACHE: dict = {}


def _atr_for(root: str) -> "float | None":
    """ATR14 for an underlying, cached per symbol per day.

    Reuses weekly_signals, which already computes True Range with the gap
    included -- the thing a stall is trying to survive. Returns None on any
    failure so the caller falls back to the percent give-back rather than
    losing the rule entirely.
    """
    key = (root, datetime.now(timezone.utc).date())
    if key in _ATR_CACHE:
        return _ATR_CACHE[key]
    val = None
    try:
        from . import weekly_signals

        val = (weekly_signals.read(root) or {}).get("atr14")
        val = float(val) if val else None
    except Exception:
        logger.warning("ATR unavailable for %s — using the percent give-back.",
                       root, exc_info=True)
    _ATR_CACHE[key] = val
    return val


def _giveback_points(root: str, entry_abs: float, peak_pct: float = 0.0,
                     flat: "float | None" = None, width: float = 0.0,
                     band: "float | None" = None,
                     atr_factor: "float | None" = None) -> float:
    """Points of RETURN that count as a give-back for this structure.

    THREE BASES, tried in the order of how well each travels between
    positions. All three return the same units -- points of return against the
    entry -- so the comparison at the call site never changes.

    A SHARE OF THE BAND -- width minus entry -- is the best of them: fixed by
    the structure at entry, so it does not drift with how far the position
    happened to run, and it is the unit stall_replay.py measures in.

    A SHARE OF THE PEAK GAIN also travels, but the peak is an accident of the
    session; the same setting means different things at +15% and +47%.

    ATR travels across ROLLS of one name, where the entry moves but the
    instrument does not. It cannot travel between instruments whose ATR and
    spread width are differently matched.

    THE FLAT PERCENT travels nowhere, and is still the default, because every
    give-back measurement on this account was taken at it. `flat` lets each
    caller keep its own -- the 0DTE stall and the later stall read different
    settings and always have.
    """
    # `band` per caller for the same reason `flat` is: the 0DTE stall and the
    # later stall are different rules and always were, and a single global
    # band quietly made them one.
    _band = ORPHAN_STALL_GIVEBACK_BAND if band is None else band
    if _band > 0 and entry_abs and width > entry_abs:
        return _band * (width - entry_abs) / entry_abs * 100.0
    if ORPHAN_STALL_GIVEBACK_FRACTION > 0 and peak_pct > 0:
        return peak_pct * ORPHAN_STALL_GIVEBACK_FRACTION
    _af = ORPHAN_LATER_STALL_GIVEBACK_ATR if atr_factor is None else atr_factor
    if _af > 0 and entry_abs:
        atr = _atr_for(root)
        if atr:
            return _af * atr / entry_abs * 100.0
    return ORPHAN_LATER_STALL_GIVEBACK_PCT if flat is None else flat


def _rolled_net(orders: list, lsym: str, ssym: str, qty: int) -> "float | None":
    """True cost basis per contract for a pair whose legs were ROLLED, or None.

    WHY THE PER-LEG PRICE IS WRONG AFTER A ROLL. The inferred path prices a
    pair as long_fill - short_fill, one price per contract symbol. That is
    correct for a pair opened as a pair. It is WRONG the moment a short leg is
    rolled, because the roll is a separate order -- buy the old short back,
    sell a new one -- and NEITHER side of it appears in the surviving legs'
    fill prices.

    Observed live 2026-09-18. A SNDK 1605/1630 opened at 10.60 had its short
    rolled to 1680: buy 1630 back at 57.00, sell 1680 at 20.70, a 36.30 debit.
    True basis 46.90. The per-leg calculation returned 27.10 - 20.70 = 6.40,
    understating it by FORTY DOLLARS and reporting +790% on the position.
    Everything downstream is a percentage OF that number:

        soft stop -10%    should be 42.21, was 5.76
        hard stop -30%    should be 32.83, was 4.48
        target +70%       should be 79.73, was 10.88

    So the position had no working stop at all -- the mark would have had to
    collapse 90% before anything fired -- and its P&L read 4,050 dollars high.

    HOW IT IS RECOVERED: walk the order history from the surviving legs and
    follow every order that shares a symbol, transitively. The roll order
    shares the new short; the original open shares the long. Summing their
    net gives the cash actually paid for the structure that is held now.

    RETURNS None RATHER THAN A GUESS when the walk finds nothing or only one
    order -- one order means no roll happened and the per-leg price is already
    right. A basis this cannot verify must not be invented, because every
    stop and target is a percentage of it.
    """
    try:
        seen, frontier = set(), {lsym, ssym}
        used, net = [], 0.0
        # SEED FROM THE CACHE FIRST. Tradier's /orders is the CURRENT SESSION
        # only, so a pair opened yesterday and rolled today has its OPEN in the
        # cached structures and its ROLL in today's orders -- neither source
        # alone can price it. The cached record for the pair being priced is
        # skipped on purpose: it may be a stale per-leg value written before
        # the roll was understood (6.40 sat in the cache while the truth was
        # 46.90), and seeding from it would launder the wrong number back in.
        me = "|".join(sorted((lsym, ssym)))
        for key, rec in (_load().get("structures") or {}).items():
            if key == me:
                continue
            # ONLY A GENUINE PRIMARY MAY SEED THE WALK. A cached pair with a
            # real `opened` timestamp is an actual fill the order window has
            # aged out of -- the overnight case this exists for. One with
            # opened=None is a SUMMARY this walk itself wrote back (inferred
            # or roll-corrected), and it already contains the earlier flows.
            # Seeding from it counts them twice: on the second roll of SNDK
            # 1605 the walk added the original 10.60, roll one 36.30, AND the
            # cached 46.90 that was the sum of those two -- 113.70 against a
            # true 66.80, turning a +33% position into a -21% one with the
            # slow-stop clock running on it.
            if not rec.get("opened"):
                continue
            syms = set(key.split("|"))
            if syms & frontier and rec.get("net") is not None:
                used.append({"id": "cache:" + key, "legs": [{"symbol": x} for x in syms],
                             "net": rec["net"], "credit": rec.get("credit", False),
                             "qty": rec.get("qty") or 1})
                frontier |= syms
        for _ in range(6):                     # depth-limited; rolls are shallow
            nxt = set()
            for o in orders:
                oid = o.get("id")
                if oid in seen:
                    continue
                syms = {l["symbol"] for l in o["legs"]}
                if not (syms & frontier):
                    continue
                seen.add(oid)
                used.append(o)
                nxt |= syms
            if not (nxt - frontier):
                break
            frontier |= nxt
        if len(used) < 2:
            return None                        # no roll; per-leg price stands
        for o in used:
            # A closing order on OTHER strikes can share no leg and is never
            # reached; one that is reached is part of this structure's history.
            sign = 1.0 if not o.get("credit") else -1.0
            net += abs(float(o["net"])) * sign * int(o.get("qty") or 1)
        per = net / max(1, qty)
        # Sanity: a basis outside the strike width is not a basis.
        return round(per, 4) if 0 < per < 1e5 else None
    except Exception:
        logger.warning("Roll-aware basis failed — falling back to per-leg "
                       "prices, which understate a rolled pair.", exc_info=True)
        return None


def open_structures(engine_symbols: "set | None" = None) -> list:
    """Reconstruct what is open, from the orders that opened it.

    Opening fills add, closing fills subtract, and whatever still has quantity
    left is open. Matched on the leg PAIR rather than on single symbols, so a
    strike used by two different spreads does not merge them.

    Cross-checked against the position list: a structure the account no longer
    holds is dropped even if the closing order was never seen, because an
    assignment or an expiry leaves no closing fill at all.
    """
    engine_symbols = engine_symbols or set()
    orders = tradier_orders.filled_spread_orders()
    # None means UNREADABLE, {} means READ AND EMPTY. The difference is the
    # whole of this fix.
    #
    # Both cross-checks below were written `not held or ...` and `if held and
    # ...`, which fails open when the position list cannot be fetched -- right,
    # because losing the broker for one cycle must not delete the book. But an
    # empty dict also means the account is genuinely FLAT, and the same
    # expression then skips the check and resurrects every structure the order
    # history has ever seen.
    #
    # Observed 2026-09-10 on a flat account: six phantom structures reported as
    # open, the oldest four days stale -- CRWV 95/99 from Monday, SNDK
    # 1610/1710 from Tuesday, SNDK 1700/1850 from Wednesday. Every one was
    # being tracked, armed and evaluated for a stall, and orphans.py places its
    # own orders without going through service._broker_holds. A phantom that
    # armed would have submitted a close for a position that does not exist.
    held = None
    try:
        held = {}
        for p in tradier_orders.open_positions():
            held[p.get("symbol")] = int(float(p.get("quantity") or 0))
    except Exception:
        held = None
        logger.exception("Position cross-check unavailable — reporting from orders alone.")

    book = {}
    # A close today that is LARGER than today's opens closed a position opened
    # in an EARLIER session (/orders is session-only). 2026-09-23: MU 1070/1080
    # x20 bought the day before, closed x20 at 10:28, re-bought x4 @ 6.05 at
    # 11:12 -- netted within the session that was -16, so the pair fell back to
    # the day-old cache and was managed as x20 @ 4.91. The excess is recorded
    # here, the session count floors at zero, and the cache below is reduced
    # by it.
    closed_prior: dict = {}
    # In time order: "a close larger than the opens so far" only means an
    # earlier session if the opens it is compared against came first.
    for o in sorted(orders, key=lambda o: str(o.get("created") or "")):
        syms = tuple(sorted(l["symbol"] for l in o["legs"]))
        if any(s in engine_symbols for s in syms):
            continue
        rec = book.get(syms)
        if rec is None:
            rec = book[syms] = {"symbols": syms, "qty": 0, "net": o["net"],
                                "credit": o["credit"], "opened": o["created"]}
        if o["opening"]:
            # Weighted so a scaled-in structure carries a true average.
            total = rec["qty"] + o["qty"]
            if total > 0:
                rec["net"] = round((rec["net"] * rec["qty"] + o["net"] * o["qty"]) / total, 4)
            rec["qty"] = total
            rec["credit"] = o["credit"]
        elif o["closing"]:
            rec["qty"] -= o["qty"]
            if rec["qty"] < 0:
                closed_prior[syms] = closed_prior.get(syms, 0) - rec["qty"]
                rec["qty"] = 0

    # OVERNIGHT HOLDS. Tradier's /orders returns the CURRENT SESSION only, so
    # a position opened yesterday has no opening fill to rebuild from today and
    # would silently manage nothing -- found on 2026-09-03 with three spreads
    # held overnight and "structures: 0". The reconstruction is therefore
    # cached the day it is made and reused for as long as the account still
    # holds both legs. Today's orders always win, so a scale-in or a partial
    # close corrects the cached copy rather than being ignored.
    cached = _load().get("structures") or {}
    for key, rec in cached.items():
        syms = tuple(key.split("|"))
        # A key already in `book` blocks the cache -- but only if it is LIVE.
        # An opened-and-closed pair leaves a qty 0 entry, which blocked the
        # cached copy of a REOPENED structure at the same strikes and then
        # dropped out itself, leaving the legs to fall through to inferred
        # pairing and be skipped for want of a price. Observed 2026-09-03 on a
        # SNDK 1500/1560 rolled twice in one session.
        if len(syms) != 2 or (syms in book and book[syms]["qty"] > 0):
            continue
        if any(s in engine_symbols for s in syms):
            continue
        if closed_prior.get(syms):
            left = int(rec.get("qty") or 0) - closed_prior[syms]
            if left <= 0:
                continue        # the earlier-session position was closed today
            rec = dict(rec, qty=left)
        net = rec.get("net", 0.0)
        # A CACHED PAIR MAY HAVE BEEN ROLLED TODAY. The cache holds what the
        # pair cost when it was first reconstructed; a roll since then is in
        # today's orders and the cached net knows nothing about it. Worse, the
        # cache can hold a pair that was ITSELF written from a bad per-leg
        # inference -- 1605|1680 sat at 6.40 while 46.90 was the truth -- and
        # served it back every cycle as if it were a fill. Re-derive from the
        # order history whenever it can be, and let it win.
        rolled = _rolled_net(orders, syms[0], syms[1], rec.get("qty") or 1)
        if rolled is not None and abs(rolled - float(net or 0)) > 0.01:
            logger.warning(
                "ORPHAN cached pair %s: cache says %.2f, the order history "
                "gives %.2f — a roll the cache never saw. Using %.2f.",
                key, float(net or 0), rolled, rolled)
            net = rolled
        book[syms] = {"symbols": syms, "qty": rec.get("qty", 0),
                      "net": net, "credit": (net < 0) if rolled is not None else rec.get("credit", False),
                      "opened": rec.get("opened")}

    # LEGS THE ORDERS COULD NOT PAIR. A position legged into across separate
    # orders looks like two unrelated singles; pair the leftovers by strike and
    # mark the result inferred. See MANAGE_INFERRED for why that flag matters.
    # QUANTITY-AWARE, not symbol-aware. A first version treated a symbol as
    # consumed if it appeared in ANY stated pair, so a SNDK 1500 held twice
    # with one contract inside a stated 1500/1540 left the OTHER contract
    # invisible -- and the 1530 short it belonged with had nothing to pair
    # against. The leftover is what the account holds MINUS what the stated
    # structures actually use.
    # COUNT ONLY PAIRS THAT SURVIVE THE HELD CHECK.
    #
    # A structure whose legs are no longer in the account is dropped below,
    # but consumed was tallied from the WHOLE book including those. On
    # 2026-09-03 a NVDA 222.5/230 had been opened and its 230 short later
    # closed; the dead pair still claimed the 222.5 long, so five contracts
    # that were free to pair with the 227.5 shorts were counted as used and
    # never became leftovers. A 2,100-dollar spread sat outside the ladder
    # with no SINGLE warning either, because nothing was left over to warn
    # about.
    def _still_held(syms):
        # held is None -> unreadable, keep everything. held is {} -> flat.
        if held is None:
            return True
        return all(abs(held.get(x, 0)) > 0 for x in syms)

    consumed = {}
    for syms, rec in book.items():
        if not _still_held(syms):
            continue
        for sym in syms:
            consumed[sym] = consumed.get(sym, 0) + rec["qty"]
    leftover = {}
    for sym, n in (held or {}).items():
        free = abs(n) - consumed.get(sym, 0)
        if free <= 0:
            continue
        n = free if n > 0 else -free
        parsed = _parse(sym)
        if parsed is None:
            continue
        root, expiry, right, strike = parsed
        leftover.setdefault((root, expiry, right), []).append((strike, sym, n))
    fills = tradier_orders.filled_legs() if leftover else {}
    for (root, expiry, right), legs_ in leftover.items():
        longs = sorted([l for l in legs_ if l[2] > 0])
        shorts = sorted([l for l in legs_ if l[2] < 0])
        li = si = 0
        while li < len(longs) and si < len(shorts):
            (lk, lsym, lq), (sk, ssym, sq) = longs[li], shorts[si]
            n = min(lq, -sq)
            lp = (fills.get(lsym) or {}).get("price")
            sp = (fills.get(ssym) or {}).get("price")
            if lp is None or sp is None:
                logger.warning(
                    # NAME THE MISSING LEG. The original message said only
                    # that a price was absent, so diagnosing it meant guessing
                    # which of the two the order window had dropped -- on
                    # 2026-09-10 a CRWV pair degraded from x5 to x1 between two
                    # previews with nothing in the log to say why.
                    #
                    # filled_legs() now falls back to cost basis, so a leg
                    # missing HERE is absent from the order window AND the
                    # position list, which is a stranger problem than a short
                    # window and needs to be visible as such.
                    "ORPHAN inferred pair %s %g/%g: no fill price for %s "
                    "(absent from the order window AND from cost basis) — "
                    "skipped, since a return without a true entry is not a "
                    "number worth acting on.",
                    root, lk, sk,
                    [sym for sym, px in ((lsym, lp), (ssym, sp)) if px is None])
            else:
                net = round(lp - sp, 4)
                rolled = _rolled_net(orders, lsym, ssym, n)
                if rolled is not None and abs(rolled - net) > 0.01:
                    logger.warning(
                        "ORPHAN inferred pair %s %g/%g: per-leg prices give "
                        "%.2f but the ORDER HISTORY gives %.2f — a leg was "
                        "rolled and its buy-back is not in the leg prices. "
                        "Using %.2f; every percentage rule depends on it.",
                        root, lk, sk, net, rolled, rolled)
                    net = rolled
                book[tuple(sorted((lsym, ssym)))] = {
                    "symbols": (lsym, ssym), "qty": n, "net": net,
                    "credit": net < 0, "opened": None, "inferred": True,
                }
            longs[li] = (lk, lsym, lq - n)
            shorts[si] = (sk, ssym, sq + n)
            if longs[li][2] == 0:
                li += 1
            if shorts[si][2] == 0:
                si += 1

    out = []
    for syms, rec in book.items():
        if rec["qty"] <= 0:
            continue
        # Still actually held? An expiry or assignment leaves no closing fill.
        if held is not None and not all(abs(held.get(s, 0)) > 0 for s in syms):
            continue
        legs = [(_parse(s), s) for s in syms]
        if any(p is None for p, _ in legs):
            continue
        root = legs[0][0][0]
        right = legs[0][0][2]
        expiry = legs[0][0][1]
        # For a debit the LONG is the leg we paid for; identify it by strike
        # and structure rather than by re-reading the order sides.
        strikes = sorted((p[3], s) for p, s in legs)
        low, high = strikes[0], strikes[1]
        # WHICH LEG IS LONG DEPENDS ON THE RIGHT, NOT JUST THE SIGN.
        #
        # This assumed calls. For a CALL debit the long is the lower strike;
        # for a PUT debit it is the HIGHER one, because a put gains as price
        # falls. Getting it backwards does not merely mislabel the row -- _mark
        # computes bid(long) - ask(short), so the value comes out NEGATIVE.
        #
        # Observed 2026-09-03 on a manual AVGO 365/355 put debit spread bought
        # for 7.88: it marked -8.40, or -206.6%. It was WATCH ONLY, which is
        # the only reason it was not stopped out instantly on a number that
        # cannot occur. Every put spread in the book had the same defect.
        is_put = right.upper() == "P"
        if rec["credit"]:
            # Bear call: short the lower strike. Bull put: short the higher.
            short_strike, short_sym = high if is_put else low
            long_strike, long_sym = low if is_put else high
        else:
            # Call debit: long the lower. Put debit: long the higher.
            long_strike, long_sym = high if is_put else low
            short_strike, short_sym = low if is_put else high
        out.append({
            "root": root, "expiry": expiry, "right": right,
            "long": long_sym, "short": short_sym,
            "long_strike": long_strike, "short_strike": short_strike,
            "qty": rec["qty"], "entry": rec["net"], "credit": rec["credit"],
            "opened": rec["opened"], "key": "|".join(syms),
            "inferred": bool(rec.get("inferred")),
        })
    return out


def _max_return_pct(st: dict) -> "float | None":
    """The most this structure can ever return, as a percent of what was risked.

    A debit spread cannot exceed its width; a credit spread cannot exceed the
    credit collected. Both are hard ceilings set by the structure, not by the
    market, which is what makes a fraction of them a meaningful place to stop.
    """
    entry = abs(st.get("entry") or 0.0)
    if entry <= 0:
        return None
    if st["credit"]:
        return 100.0
    width = abs(st["short_strike"] - st["long_strike"])
    if width <= 0:
        return None
    return (width - entry) / entry * 100.0


def _expires_today(st: dict) -> bool:
    """Does this structure expire in the current New York session?

    The expiry is the YYMMDD from the OCC symbol, compared against the New
    York date rather than UTC -- after 20:00 ET the two disagree, and the
    disagreement would silently change which positions are in scope.
    """
    try:
        from zoneinfo import ZoneInfo
        return st.get("expiry") == datetime.now(ZoneInfo("America/New_York")).strftime("%y%m%d")
    except Exception:
        return False


def _past_force_close() -> bool:
    """Is it past the orphan flatten time, in New York?"""
    if not ORPHAN_FORCE_CLOSE:
        return False
    try:
        from zoneinfo import ZoneInfo
        hh, mm = (int(x) for x in ORPHAN_FORCE_CLOSE.split(":"))
        now = datetime.now(ZoneInfo("America/New_York"))
        return (now.hour, now.minute) >= (hh, mm)
    except Exception:
        return False


def _past_hold_until(setting: "str | None" = None) -> bool:
    """Is it past the opening quiet period, in New York? See ORPHAN_HOLD_UNTIL.

    `setting` lets the weekly ladder ask about ITS window
    (ORPHAN_LATER_HOLD_UNTIL); the default is the 0DTE one.

    Fails OPEN on a bad value -- an unparseable time leaves the stop and the
    stall working as they do today rather than silently disabling two exit
    rules for a whole session.
    """
    hold = ORPHAN_HOLD_UNTIL if setting is None else setting
    if not hold:
        return True
    try:
        from zoneinfo import ZoneInfo
        hh, mm = (int(x) for x in hold.split(":"))
        now = datetime.now(ZoneInfo("America/New_York"))
        return (now.hour, now.minute) >= (hh, mm)
    except Exception:
        return True


def _quotes_tradeable(q: dict, st: dict, reason: str = "") -> bool:
    """Is the short leg quoted well enough to act on, for THIS reason?

    The guard exists so the manager does not close a position on a garbage
    quote -- a leg quoted 1.00/2.50 is not a market, and acting on its mid
    books an imaginary price. Two things were wrong with how it did that.

    IT JUDGED BOTH LEGS. On any 0DTE position in its last hour the far leg is
    bid 0.00 because it is worthless, which is the correct quote rather than a
    broken one. Observed 2026-09-03 on a QQQ 719/722 credit: the 722 quoted
    0.00/0.01, the guard refused, and the FORCE_CLOSE decided at 15:45 logged
    "[observation only]" every cycle to the bell. So the test is on the SHORT
    leg -- the one that must be bought back, and the one carrying assignment
    risk. A long bid at zero is information, not an unusable quote, and the
    mark already values it at zero, which is right.

    IT JUDGED WIDTH ONLY IN PROPORTION. That same short quoted 0.02/0.03 is a
    ONE CENT market, as tight as an option gets, and 40% of its own mid. A
    percentage threshold rejects every cheap option, and by the close every
    option in a 0DTE book is cheap. So a quote passes if it is tight in cents
    OR tight in proportion, whichever is kinder.

    AND THE STANDARD DEPENDS ON WHAT IS BEING DECIDED. A width test protects
    a judgment about VALUE -- ceiling, stall, stop -- all of which read the
    mark and would act on a false one. FORCE_CLOSE is not a judgment about
    value: it is there because these spreads are physically settled and an
    in-the-money short gets assigned. A poor fill is worse than a good fill
    and far better than assignment, so the force close needs only a real
    market to trade into, not a tight one.
    """
    row = q.get(st["short"]) or {}
    try:
        bid, ask = float(row.get("bid") or 0.0), float(row.get("ask") or 0.0)
    except (TypeError, ValueError):
        logger.warning(
            "ORPHAN quote unreadable: %s bid=%r ask=%r — reporting but not "
            "acting.", st["short"], row.get("bid"), row.get("ask"))
        return False
    # The ask, not the bid: closing this structure BUYS the short back. A zero
    # bid on a nearly-worthless short is normal and does not stop anything; a
    # zero ask means there is no offer to buy at, which does.
    if ask <= 0:
        logger.warning(
            "ORPHAN no offer on %s (bid %.2f, ask %.2f) — nothing to buy the "
            "short back at, so %s is reported and not acted on.",
            st["short"], bid, ask, reason or "the ladder")
        return False
    if ask < bid:
        logger.warning(
            "ORPHAN crossed quote on %s: bid %.2f is ABOVE ask %.2f — that is "
            "not a market, so %s is reported and not acted on.",
            st["short"], bid, ask, reason or "the ladder")
        return False
    if reason == "FORCE_CLOSE":
        return True
    mid = (bid + ask) / 2.0
    if mid <= 0:
        return False
    if (ask - bid) > ORPHAN_MAX_LEG_SPREAD_ABS and (ask - bid) / mid > ORPHAN_MAX_LEG_SPREAD:
        logger.warning(
            "ORPHAN quote unusable: %s bid %.2f ask %.2f is %.0f%% of mid — "
            "reporting but not acting.",
            st["short"], bid, ask, (ask - bid) / mid * 100.0,
        )
        return False
    return True


def _decompose(st: dict, value: float) -> "tuple | None":
    """(intrinsic, extrinsic) for a spread, or None without a spot.

    WHY THIS EXISTS. A deep in-the-money spread marks far below what it is
    worth, and the reason is invisible in the return alone. On 2026-09-03 a
    SNDK 1500/1540 with spot at 1557.60 held its FULL 40-dollar width in
    intrinsic value and marked at 27.80, reading +15.6% against a possible
    +66.3%. Nothing was wrong with it: the short 1530 sits nearer the money
    than the long 1500 and so carries more time premium, and that premium is
    subtracted from the spread.

    Extrinsic goes to zero at expiry. So a NEGATIVE extrinsic on an ITM spread
    is not a loss, it is the part that comes back -- and telling that apart
    from a real adverse move is exactly what a bare percentage cannot do.

    Intrinsic is capped at the width: a spread cannot be worth more than the
    distance between its strikes however far past them the underlying runs.
    """
    try:
        spot = tradier_orders.quotes([st["root"]]).get(st["root"], {})
        px = float(spot.get("last") or spot.get("bid") or 0.0)
        if px <= 0:
            return None
        lo, hi = sorted((st["long_strike"], st["short_strike"]))
        width = hi - lo
        if st["right"].upper() == "P":
            intrinsic = max(0.0, min(hi - px, width))
        else:
            intrinsic = max(0.0, min(px - lo, width))
        # NO INVERSION FOR CREDIT. open_structures assigns the SHORT leg to the
        # low strike on a call credit and the high strike on a put credit, so
        # `lo`/`hi` above already resolve to the short strike and the formula
        # already yields the cost to buy the structure back. Subtracting it
        # from the width flipped max profit into max loss.
        #
        # Caught live 2026-09-03 on a QQQ 719/722 call credit, ten lots, QQQ at
        # 718.74 -- below the short, so both legs expire worthless and the cost
        # to close is ZERO. It reported 3.00, the maximum loss, which made the
        # stall read roughly -711% and SUPPRESSED the stop, because intrinsic
        # 3.00 exceeded the 0.37 entry and looked profitable at expiry. Both
        # rules were pointed the wrong way on a 0DTE position.
        return round(intrinsic, 2), round(value - intrinsic, 2)
    except Exception:
        return None


def _mark(st: dict) -> "tuple | None":
    """(value, return_pct) at prices we could actually transact at.

    Return is against the REAL entry price from the fills. For a debit that is
    (value - paid) / paid; for a credit, (collected - cost to close) /
    collected -- the same convention the engine uses for its own windows, so
    the thresholds mean the same thing in both places.
    """
    try:
        q = tradier_orders.quotes([st["long"], st["short"]])
        lq, sq = q.get(st["long"]), q.get(st["short"])
        if not lq or not sq:
            return None
        if st["credit"]:
            # Cost to buy the structure back: pay the ask on the short, receive
            # the bid on the long wing.
            cost = float(sq.get("ask") or 0.0) - float(lq.get("bid") or 0.0)
            collected = abs(st["entry"])
            if collected <= 0:
                return None
            return cost, (collected - cost) / collected * 100.0
        value = float(lq.get("bid") or 0.0) - float(sq.get("ask") or 0.0)
        paid = st["entry"]
        if paid <= 0:
            return None
        return value, (value - paid) / paid * 100.0
    except Exception:
        return None


_DEAD = {"rejected", "canceled", "cancelled", "expired", "error"}


def _fill_value(order_id) -> "tuple | None":
    """(value per spread, contracts filled) for the close, or None if unknown.

    THE QUANTITY MATTERS AS MUCH AS THE PRICE. A close can fill in parts, and
    _book used to record st["qty"] -- the size of the whole structure --
    whatever actually filled. On 2026-09-03 a QQQ 714/716 credit spread of ten
    contracts closed as two orders of five, and each wrote a row claiming ten:
    170 dollars booked twice against a real 170 total. Both rows were genuine
    closes; only the quantity was wrong.

    submit_vertical answering {'status': 'ok'} means accepted, not filled --
    the distinction that cost a false TradeHistory row on 2026-08-27 and, in
    the other direction, understated the first live orphan close by 175
    dollars on 2026-09-03: CRWV booked at its 4.20 mark having filled at 4.55.

    The mark is the NATURAL, deliberately pessimistic because it is what an
    exit pays in the worst case. The fill is what happened. Booking the mark
    makes every orphan result wrong in the same direction, and that number
    feeds the daily loss limit and the consecutive-loss breaker.
    """
    if not order_id:
        return None
    for _ in range(6):
        try:
            o = tradier_orders.order_status(order_id)
            status = (o.get("status") or "").lower()
            if status == "filled":
                legs = o.get("leg")
                if not isinstance(legs, list):
                    return None
                total, qty = 0.0, None
                for lg in legs:
                    px = float(lg.get("avg_fill_price") or 0.0)
                    total += -px if (lg.get("side") or "").lower().startswith("buy") else px
                    n = int(float(lg.get("exec_quantity") or 0))
                    qty = n if qty is None else min(qty, n)
                if not qty:
                    return None
                return abs(round(total, 4)), qty
            if status in _DEAD:
                logger.error("ORPHAN close %s came back %s — not booking.", order_id, status)
                return None
        except Exception:
            logger.exception("Could not read orphan close %s.", order_id)
            return None
        time.sleep(2)
    logger.warning("ORPHAN close %s still working — booking deferred.", order_id)
    return None


def _send_close(st: dict, reason: str, limit_price: float) -> "tuple | None":
    """Send the closing order; return (order id, contracts sent), or None.

    _close wraps this and waits for the fill. The ask path calls it directly,
    because an ask is MEANT to rest.

    None means it did not fill -- rejected, or still working. The caller must
    not book a result for it: the position is either still open or never left,
    and the next pass will see it again.
    """
    try:
        # SAME CHECK service._broker_holds() MAKES, because this path never
        # goes through service.py. On 2026-09-10 phantom structures from a
        # flat-account bug submitted a rejected close every minute, and the
        # guard added to the engine's exit that morning did nothing here.
        try:
            _pos = tradier_orders.open_positions() or []
            _held = {p.get("symbol") for p in _pos}
            # HOW MANY, not just whether. open_structures() pairs by expiry and
            # right, so after a PARTIAL close its qty is stale -- and submitting
            # a close for more contracts than are held is rejected outright.
            #
            # Observed live 2026-09-18: one of two SNDK 1600/1640 was closed by
            # hand, the broker showed 1600 x1 and 1640 x-1, and the engine still
            # reported x2. Any stop firing would have sent a 2-lot close against
            # a 1-lot holding and been refused -- "submitted" in the log, the
            # position still open, which is the worst possible outcome for an
            # exit rule.
            #
            # /trading/flatten got exactly this clamp on 2026-09-10 after
            # "SNDK 1750/1800 x5" was reported against three held. Same bug,
            # second place, and only one of them was fixed.
            _n = {}
            for _p in _pos:
                try:
                    _n[str(_p.get("symbol"))] = abs(int(float(_p.get("quantity") or 0)))
                except (TypeError, ValueError):
                    continue
            _have = min(_n.get(st["long"], 0), _n.get(st["short"], 0))
            if _have and _have < int(st["qty"]):
                logger.warning(
                    "ORPHAN %s %g/%g: pairing says x%d but the broker holds x%d "
                    "— closing %d. A close for more than is held is rejected, "
                    "and a rejected close reads as success.",
                    st["root"], st["long_strike"], st["short_strike"],
                    int(st["qty"]), _have, _have,
                )
                st = dict(st, qty=_have)
            if _held and not ({st["long"], st["short"]} & _held):
                logger.warning(
                    "ORPHAN %s %g/%g: the broker holds neither leg — not "
                    "sending a close. Reconstruction says open, the account "
                    "says otherwise.",
                    st["root"], st["long_strike"], st["short_strike"])
                return None
        except Exception:
            logger.warning("Could not verify holdings before an orphan close "
                           "— letting it through.", exc_info=True)

        res = tradier_orders.submit_vertical(
            st["root"],
            f"20{st['expiry'][:2]}-{st['expiry'][2:4]}-{st['expiry'][4:6]}",
            "call" if st["right"].upper() == "C" else "put",
            long_strike=st["long_strike"], short_strike=st["short_strike"],
            quantity=st["qty"], opening=False,
            limit_price=abs(limit_price), is_credit=st["credit"],
        )
        # LEVEL BY OUTCOME, and reaching this line IS the good outcome:
        # _post_order raises OrderError on every Tradier rejection, so a
        # failure never gets here -- it goes to the logger.exception below.
        # This logged at ERROR regardless, so `grep ERROR` returned mostly
        # successful closes and the rejections worth finding sat in the same
        # bucket as the fills. SUPPRESSED is the one case that is neither:
        # nothing was sent because TRADING_LIVE_ORDERS is off, and a position
        # the engine believes it closed is still open at the broker.
        suppressed = str((res or {}).get("status") or "").lower() == "suppressed"
        logger.log(
            logging.WARNING if suppressed else logging.INFO,
            "ORPHAN %s: %s %s %g/%g x%d — %s",
            reason, "NOT closing (orders suppressed)" if suppressed else "closing",
            st["root"], st["long_strike"], st["short_strike"], st["qty"], res)
        return (res or {}).get("id"), int(st["qty"])
    except Exception:
        logger.exception("ORPHAN close failed for %s — position left open.", st["key"])
        return None


def _close(st: dict, reason: str, limit_price: float) -> "tuple | None":
    """Send the closing order and wait; return (filled value, contracts), or None.

    None means it did not fill -- rejected, or still working. The caller must
    not book a result for it: the position is either still open or never left,
    and the next pass will see it again.
    """
    sent = _send_close(st, reason, limit_price)
    if not sent:
        return None
    return _fill_value(sent[0])


def _past_clock(hhmm: str) -> bool:
    """Is it at or past HH:MM New York time? Empty or unparseable -> False."""
    if not hhmm:
        return False
    try:
        from zoneinfo import ZoneInfo
        hh, mm = (int(x) for x in hhmm.split(":"))
        now = datetime.now(ZoneInfo("America/New_York"))
        return (now.hour, now.minute) >= (hh, mm)
    except Exception:
        return False


_VWAP_CACHE: dict = {}


def _session_vwap(root: str) -> "float | None":
    """Session VWAP of the underlying from Tradier's 5-minute bars, cached 60 s.

    Same construction as data_feed._tradier_session_vwap, which is QQQ-only:
    the volume-weighted mean of the per-bar vwap Tradier already returns,
    regular hours only. None on any failure, and the caller HOLDS on None --
    a missing read must never step an ask down.
    """
    import httpx
    from zoneinfo import ZoneInfo
    hit = _VWAP_CACHE.get(root)
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    out = None
    try:
        today = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
        r = httpx.get(f"{tradier_orders._base()}/markets/timesales",
                      params={"symbol": root, "interval": "5min",
                              "start": f"{today} 09:30", "end": f"{today} 16:00",
                              "session_filter": "open"},
                      headers=tradier_orders._headers(), timeout=10.0)
        r.raise_for_status()
        data = ((r.json() or {}).get("series") or {}).get("data") or []
        if isinstance(data, dict):
            data = [data]
        num = den = 0.0
        for bar in data:
            vol, vw = bar.get("volume"), bar.get("vwap")
            if not vol or vw is None:
                continue
            num += float(vw) * float(vol)
            den += float(vol)
        out = num / den if den > 0 else None
    except Exception:
        out = None
    _VWAP_CACHE[root] = (time.time(), out)
    return out


def _tape_from_bars(data: list, spot: "float | None" = None,
                    slope_bars: "int | None" = None) -> "tuple | None":
    """(spot, vwap_now, vwap_ref) from Tradier 5-minute bars, or None.

    vwap_now is the running session VWAP at the last bar; vwap_ref the same
    series `slope_bars` bars earlier (the first bar when the session is
    younger than that). Pure, so the tape exit's arithmetic can be tested.
    """
    n = ORPHAN_TAPE_EXIT_SLOPE_BARS if slope_bars is None else slope_bars
    num = den = 0.0
    running = []
    last_close = None
    for bar in data or []:
        vol, vw = bar.get("volume"), bar.get("vwap")
        if not vol or vw is None:
            continue
        num += float(vw) * float(vol)
        den += float(vol)
        running.append(num / den)
        try:
            last_close = float(bar.get("close"))
        except (TypeError, ValueError):
            pass
    if not running:
        return None
    px = spot if spot is not None else last_close
    if px is None:
        return None
    ref = running[-1 - n] if len(running) > n else running[0]
    return float(px), running[-1], ref


def _tape_against(right: str, spot: float, vwap_now: float, vwap_ref: float) -> bool:
    """Is the session tape walking away from a DEBIT structure of this right?

    A call debit is bullish: wrong side is UNDER the VWAP, moving against is
    the VWAP FALLING. A put debit is the mirror. Both conditions, not either.
    """
    if right == "C":
        return spot < vwap_now and vwap_now < vwap_ref
    if right == "P":
        return spot > vwap_now and vwap_now > vwap_ref
    return False


_TAPE_CACHE: dict = {}


def _session_tape(root: str) -> "tuple | None":
    """(spot, vwap_now, vwap_ref) for the underlying right now, cached 60 s.
    None on any failure; the caller must treat None as 'no reading', not as
    'the tape agrees'."""
    import httpx
    from zoneinfo import ZoneInfo
    hit = _TAPE_CACHE.get(root)
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    out = None
    try:
        today = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
        r = httpx.get(f"{tradier_orders._base()}/markets/timesales",
                      params={"symbol": root, "interval": "5min",
                              "start": f"{today} 09:30", "end": f"{today} 16:00",
                              "session_filter": "open"},
                      headers=tradier_orders._headers(), timeout=10.0)
        r.raise_for_status()
        data = ((r.json() or {}).get("series") or {}).get("data") or []
        if isinstance(data, dict):
            data = [data]
        spot = None
        try:
            from .data_feed import fetch_spot
            spot = fetch_spot(root)
        except Exception:
            spot = None
        out = _tape_from_bars(data, spot)
    except Exception:
        out = None
    _TAPE_CACHE[root] = (time.time(), out)
    return out


def _ask_cancel(ask: dict, st: dict) -> str:
    """Cancel a resting ask and say what became of it: 'canceled', 'filled', 'unknown'.

    'filled' means the market took it between our decision and the cancel;
    the caller must book it, not replace it. 'unknown' means the broker did
    not confirm either way inside four seconds, and the caller must NOT send
    another order -- that is exactly how a spread gets sold twice.
    """
    oid = str(ask.get("id"))
    try:
        tradier_orders.cancel_order(oid)
    except Exception:
        logger.exception("ORPHAN ASK %s %g/%g: cancel of order %s failed.",
                         st["root"], st["long_strike"], st["short_strike"], oid)
    for _ in range(8):
        try:
            status = (tradier_orders.order_status(oid).get("status") or "").lower()
        except Exception:
            status = ""
        if status == "filled":
            return "filled"
        if status in _DEAD:
            return "canceled"
        time.sleep(0.5)
    return "unknown"


def _ask_manage(st: dict, rec: dict, value: "float | None", entry_abs: float,
                width: float, own_ask_working: bool) -> None:
    """Place, hold, step, or withdraw the resting ask for one structure.

    Called only when nothing else in the ladder wants to act this pass and
    the structure is manageable. Mutates rec["ask"], which is persisted with
    the peaks so a restart neither forgets a working order nor places a
    second one.
    """
    if not ORPHAN_ASK or st["credit"] or width <= 0 or not entry_abs:
        return
    if ORPHAN_ASK_FLOOR_WIDTH > 0:
        floor = width * ORPHAN_ASK_FLOOR_WIDTH
    elif ORPHAN_TARGET_RETURN_PCT > 0:
        floor = entry_abs * (1.0 + ORPHAN_TARGET_RETURN_PCT / 100.0)
    else:
        return
    if floor <= entry_abs or floor >= width:
        return          # no room between cost and the width: nothing to ask for
    floor = round(floor, 2)
    start = max(round(width * ORPHAN_ASK_START_WIDTH, 2), floor)
    ask = rec.get("ask") if isinstance(rec.get("ask"), dict) else None
    now = datetime.now(timezone.utc)
    tag = "ORPHAN ASK %s %g/%g" % (st["root"], st["long_strike"], st["short_strike"])

    if ask and own_ask_working:
        if _past_clock(ORPHAN_ASK_CANCEL_BY):
            outcome = _ask_cancel(ask, st)
            logger.info("%s: %s — withdrawing the %.2f ask (%s); the flatten takes it from here.",
                        tag, ORPHAN_ASK_CANCEL_BY, ask["price"], outcome)
            if outcome != "filled":
                rec["ask"] = None
            return
        if not _past_clock(ORPHAN_ASK_VWAP_FROM):
            return
        last = datetime.fromisoformat(ask.get("last_step") or ask["placed"])
        if (now - last).total_seconds() < ORPHAN_ASK_STEP_MINUTES * 60:
            return
        if ask["price"] <= floor + 1e-9:
            return          # at the floor: it fills or the ladder acts
        from .data_feed import fetch_spot
        spot = fetch_spot(st["root"])
        vwap = _session_vwap(st["root"])
        if spot is None or vwap is None:
            logger.info("%s: no spot/VWAP read (%s / %s) — holding the %.2f ask.",
                        tag, spot, vwap, ask["price"])
            return
        if spot >= vwap:
            logger.info("%s: %s %.2f at or above VWAP %.2f — holding the %.2f ask.",
                        tag, st["root"], spot, vwap, ask["price"])
            return
        new = max(round(ask["price"] - ORPHAN_ASK_STEP, 2), floor)
        outcome = _ask_cancel(ask, st)
        if outcome == "filled":
            return          # the next pass books it
        if outcome == "unknown":
            logger.warning("%s: cancel of the %.2f ask unconfirmed — not replacing it this pass.",
                           tag, ask["price"])
            return
        sent = _send_close(st, "ASK", new)
        if sent and sent[0]:
            rec["ask"] = {"id": str(sent[0]), "price": new, "qty": sent[1],
                          "placed": ask["placed"], "last_step": now.isoformat(),
                          "steps": int(ask.get("steps", 0)) + 1, "floor": floor}
            logger.info("%s: %s %.2f under VWAP %.2f — stepped %.2f -> %.2f (floor %.2f, step %d).",
                        tag, st["root"], spot, vwap, ask["price"], new, floor, rec["ask"]["steps"])
        else:
            rec["ask"] = None
            logger.warning("%s: replacement at %.2f was not accepted — the ask is off; the ladder resumes.",
                           tag, new)
        return

    if ask:
        return              # recorded but not confirmed working: do not stack a second order
    if value is None or value >= start:
        return              # already bid at the start: TARGET sells at the mark
    if _past_clock(ORPHAN_ASK_CANCEL_BY):
        return
    sent = _send_close(st, "ASK", start)
    if sent and sent[0]:
        rec["ask"] = {"id": str(sent[0]), "price": start, "qty": sent[1],
                      "placed": now.isoformat(), "last_step": now.isoformat(),
                      "steps": 0, "floor": floor}
        logger.info("%s x%d: pinned at the %g width, bid %.2f — resting a sell at %.2f "
                    "(%.0f%% of width). Steps %.2f every %.0f min while %s is under "
                    "VWAP, from %s; floor %.2f; withdrawn at %s.",
                    tag, sent[1], width, value, start, 100 * start / width,
                    ORPHAN_ASK_STEP, ORPHAN_ASK_STEP_MINUTES, st["root"],
                    ORPHAN_ASK_VWAP_FROM, floor, ORPHAN_ASK_CANCEL_BY)


def _book(st: dict, value: float, ret_pct: float, reason: str,
          qty: "int | None" = None) -> None:
    """Write a TradeHistory row so manual results reach the circuit breakers.

    Without this, a manual loss is invisible to the daily loss limit and the
    consecutive-loss breaker -- both read TradeHistory, which only ever
    recorded trades the engine itself closed. On 2026-09-02 a -657.50 manual
    loss counted toward neither, and the engine's risk budget was untouched by
    it.
    """
    try:
        from config.db_pgrs import SessionLocal
        from models_pgdb.trading_models import TradeHistory
        entry = abs(st["entry"])
        # The contracts that actually FILLED, not the structure's size.
        n = qty or st["qty"]
        pnl = ((entry - value) if st["credit"] else (value - entry)) * n * 100
        db = SessionLocal()
        try:
            # THE LABEL HAS TO NAME THE SIDE, and this one did not.
            #
            # It read `"CALL_CREDIT_SPREAD" if credit else "BULL_CALL_SPREAD"`,
            # which distinguishes credit from debit and nothing else -- so
            # EVERY debit spread was booked as BULL_CALL_SPREAD, put debits
            # included. On 2026-09-14 that made the whole day's history read as
            # bull call spreads: AVGO 350/342 puts, MU 930/925 puts, TSLA
            # 365/358 puts, all labelled bullish calls.
            #
            # It is not cosmetic. It is the column anyone groups by to ask
            # "how did put spreads do against call spreads", which was exactly
            # the question that day raised, and the answer it gave was
            # "there were no put spreads". st["right"] has carried C/P the
            # whole time.
            call = st["right"].upper() == "C"
            strategy = ("CALL_CREDIT_SPREAD" if (call and st["credit"])
                        else "PUT_CREDIT_SPREAD" if st["credit"]
                        else "BULL_CALL_SPREAD" if call
                        else "BEAR_PUT_SPREAD")
            db.add(TradeHistory(
                strategy=strategy,
                underlying=st["root"], quantity=n,
                long_strike=st["long_strike"], short_strike=st["short_strike"],
                entry_net_debit=entry, exit_net_value=value,
                realized_pnl_dollars=round(pnl, 2), realized_pnl_pct=round(ret_pct, 2),
                close_reason=reason, playbook="MANUAL",
                # OPENED_AT WAS NEVER PASSED, so every row had a close time and
                # no open time. Without it a trade cannot be placed in the
                # session -- "what did entries taken after 11:00 do" is
                # unanswerable, and on 2026-09-14 it had to be reconstructed
                # from log timestamps and could not be pinned.
                #
                # st["opened"] is None for an INFERRED pairing, where the
                # engine matched two legs it did not open. None is the honest
                # answer there; a guess would be worse than a null.
                opened_at=st.get("opened"),
            ))
            db.commit()
            logger.info("ORPHAN booked to history: %s %g/%g x%d %+.2f (%s)",
                        st["root"], st["long_strike"], st["short_strike"],
                        n, pnl, reason)
        finally:
            db.close()
    except Exception:
        logger.exception("Could not book orphan result — it will not reach the loss limit.")


def review(engine_symbols: "set | None" = None) -> list:
    """Mark every structure the engine did not open, and say what the ladder says.

    Returns the structures it reported. Never raises: this must not be able to
    break a trading cycle.
    """
    if not WATCH_ORPHANS:
        return []
    try:
        structures = open_structures(engine_symbols)
        state = _load()
        peaks = state["peaks"]
        now = datetime.now(timezone.utc)
        seen, reported = set(), []
        # Cache today's reconstruction so tomorrow can still price a position
        # held overnight. See the note in open_structures.
        # MERGE, DO NOT REPLACE.
        #
        # This used to rebuild the cache from whatever open_structures returned
        # this pass, which silently erased any entry it could not reproduce --
        # including a hand-seeded one, within a minute. On 2026-09-03 a SNDK
        # 1500/1560 could not be priced (the 1500 was bought the previous
        # session, and /orders is session-only, so today's fills net its
        # quantity to zero) and every attempt to seed its entry was wiped by
        # the next cycle.
        #
        # The cache is a RECORD, not a mirror of the current pass. Entries are
        # dropped when a structure closes -- handled at the close site -- not
        # because one pass failed to rebuild it.
        for st in structures:
            # A DIFFERENT position on the same legs (re-bought after a close,
            # or resized by hand) must not inherit the old one's peak: the
            # stall and give-back read it. Reset and say so.
            prev = state["structures"].get(st["key"])
            if prev and (int(prev.get("qty") or 0) != int(st["qty"])
                         or abs(float(prev.get("net") or 0) - float(st["entry"])) > 0.01):
                old_rec = peaks.pop(st["key"], None)
                if old_rec is not None:
                    # KEEP THE ENGINE'S OWN ORDER. rec["ask"] is how the engine
                    # knows a working order on these legs is its own resting
                    # ask; dropping it with the peak made that order look like
                    # a stranger's and switched the position to observation
                    # only (2026-09-23 11:17, MU 1070/1080's 9.00 ask).
                    if isinstance(old_rec.get("ask"), dict):
                        peaks[st["key"]] = {"ask": old_rec["ask"]}
                    logger.info("ORPHAN %s: now x%s @ %.2f (was x%s @ %.2f) — a different "
                                "position on these legs; its peak starts over.",
                                st["key"], st["qty"], abs(float(st["entry"])),
                                prev.get("qty"), abs(float(prev.get("net") or 0)))
            state["structures"][st["key"]] = {
                "qty": st["qty"], "net": st["entry"],
                "credit": st["credit"], "opened": st["opened"],
            }

        # THE ACCOUNT-LEVEL CHECK, ONCE PER PASS AND BEFORE ANY POSITION IS
        # LOOKED AT. See ACCOUNT_FLOOR.
        floor_breached = False
        if ACCOUNT_FLOOR > 0:
            try:
                snap = tradier_orders.account_snapshot() or {}
                equity = float(snap.get("total_equity") or 0.0)
                # A zero or unreadable equity is NOT a breach. Failing open
                # here matters more than anywhere else in this file: a broker
                # hiccup must not liquidate the book.
                if equity > 0 and equity <= ACCOUNT_FLOOR:
                    floor_breached = True
                    logger.error(
                        "ACCOUNT FLOOR BREACHED: equity %.2f is at or below %.2f — "
                        "flattening every position regardless of expiry.",
                        equity, ACCOUNT_FLOOR,
                    )
            except Exception:
                logger.exception("Could not read equity — floor check skipped this pass.")

        # ONE broker call for the whole pass, not one per structure. See the
        # in_flight check below.
        try:
            working = tradier_orders.working_leg_symbols()
        except Exception:
            logger.exception("Could not read working orders — proceeding without the guard.")
            working = set()
        if working:
            logger.info("ORPHAN %d leg(s) have orders working: %s",
                        len(working), ", ".join(sorted(working)))

        for st in structures:
            key = st["key"]
            seen.add(key)
            mark = _mark(st)
            if mark is None:
                continue
            value, ret_pct = mark

            # WHICH RETURN THE STALL WATCHES.
            #
            # On an in-the-money spread the mark is the wrong series: it fades
            # as time premium bleeds out of the short leg, which is the profit
            # ARRIVING, not leaving. Track the peak on intrinsic-vs-entry when
            # that is available, so a stall fires on a real reversal and not on
            # decay. Falls back to the mark when intrinsic cannot be computed.
            parts_iv = _decompose(st, value)
            entry_abs = abs(st["entry"])
            stall_pct = ret_pct
            if STALL_RESPECTS_INTRINSIC and parts_iv and entry_abs:
                stall_pct = ((entry_abs - parts_iv[0]) if st["credit"]
                             else (parts_iv[0] - entry_abs)) / entry_abs * 100.0

            # THE PEAK IS STORED IN DOLLARS OF INTRINSIC, NOT AS A RETURN
            # PERCENT, BECAUSE THE PERCENT'S BASELINE MOVES.
            #
            # stall_pct is measured against the ENTRY, and the entry changes
            # whenever contracts are added to a structure that is already
            # open. Observed live 2026-09-04: SNDK 1700/1720 was scaled up and
            # its average entry went 11.60 -> 13.20, so the SAME untouched
            # 20.00 of intrinsic re-read as 72.4% and then 51.5%. Against a
            # stored peak of 72.4 that is a 20.9-point giveback, and the stall
            # would have booked a position that had not moved at all.
            #
            # Intrinsic in dollars has no baseline to shift. It changes when
            # the underlying changes, which is the only thing the stall is
            # trying to detect. The percent is still derived for the log line
            # and for the giveback thresholds, but it is derived FROM the
            # dollar peak against the CURRENT entry, so both sides of the
            # comparison always use the same basis.
            iv_now = parts_iv[0] if parts_iv else None
            rec = peaks.get(key) or {}
            prev_iv = rec.get("peak_iv")
            if prev_iv is None and rec.get("peak") is not None and entry_abs:
                # MIGRATING A RECORD WRITTEN BEFORE peak_iv EXISTED.
                #
                # The first version of this discarded the old peak outright.
                # Deploying it mid-session on 2026-09-04 reseeded every open
                # structure from its then-current intrinsic and wiped +20.0%
                # to -9.2% across the whole book at once -- a session's
                # high-water marks destroyed by a deploy, with the stall left
                # blind for the rest of the day.
                #
                # The stored percent IS recoverable when the entry that
                # produced it has not moved, which is the ordinary case. So
                # the entry is now recorded alongside, and the peak is
                # reconstructed only when it still matches. A structure that
                # has been scaled since -- the case that motivated storing
                # dollars in the first place -- still reseeds, because there
                # its percent genuinely means nothing.
                if abs(float(rec.get("peak_entry") or 0.0) - entry_abs) < 0.005:
                    pct = float(rec["peak"])
                    prev_iv = (entry_abs * (1.0 - pct / 100.0) if st["credit"]
                               else entry_abs * (1.0 + pct / 100.0))
            better = (iv_now is not None
                      and (prev_iv is None
                           or (iv_now < prev_iv if st["credit"] else iv_now > prev_iv)))
            if better:
                # UPDATE, DO NOT REPLACE. This used to build a fresh dict, which
                # threw away everything else the record carries -- the engine's
                # own resting ask, the strike guard's arming, every rule clock.
                # 2026-09-23 11:22: MU 1065/1075's 8.00 ask filled x10 and was
                # never booked, because a new peak had dropped rec["ask"].
                rec = dict(rec, peak_iv=iv_now, peak_at=now.isoformat(),
                           peak_entry=entry_abs)
                rec.pop("peak", None)
            elif not rec:
                rec = {"peak_iv": iv_now, "peak_at": now.isoformat(),
                       "peak_entry": entry_abs}
            rec.setdefault("peak_entry", entry_abs)
            rec.setdefault("peak_at", now.isoformat())
            peak_iv = rec.get("peak_iv")
            # Derived, never stored: the percent this peak represents against
            # the entry as it stands right now.
            if peak_iv is not None and entry_abs:
                rec["peak"] = (((entry_abs - peak_iv) if st["credit"]
                                else (peak_iv - entry_abs)) / entry_abs * 100.0)
            else:
                rec["peak"] = rec.get("peak", stall_pct)
            peaks[key] = rec
            quiet = (now - datetime.fromisoformat(rec["peak_at"])).total_seconds() / 60.0
            stop_pct = ORPHAN_CREDIT_STOP_PCT if st["credit"] else ORPHAN_STOP_PCT

            max_ret = _max_return_pct(st)
            ceiling = (ORPHAN_CEILING_FRACTION * max_ret
                       if (max_ret and ORPHAN_CEILING_FRACTION > 0) else None)

            # WHICH RULES ARE ACTUALLY ABOUT THE EXPIRY, AND WHICH ARE NOT.
            #
            # The stop, the force close and the stall all assume the position
            # has hours rather than days: a multi-day drawdown can recover, a
            # Friday spread flattened on Wednesday is closed for no reason, and
            # a multi-day thesis should not die on one quiet five minutes. They
            # are 0DTE rules and stay scoped to the expiry day.
            #
            # THE CEILING IS NOT. A structure worth 90% of its maximum has the
            # same tiny upside left whenever it expires -- and holding the full
            # width of risk for the last few cents is WORSE over two days than
            # over two hours, not better. So the ceiling applies at any expiry.
            expires_today = _expires_today(st)
            zero_dte = (not ORPHAN_TODAY_ONLY) or expires_today

            # Intrinsic is computed once here: the stop guard reads it, and so
            # does the log line below.
            intrinsic_ok = False
            if parts_iv and STOP_RESPECTS_INTRINSIC:
                # THE COMPARISON REVERSES FOR A CREDIT STRUCTURE.
                #
                # For a debit, intrinsic is the VALUE at expiry: above what was
                # paid means profitable, so a mark-based stop should not fire.
                # For a credit, intrinsic is the COST TO CLOSE: above the credit
                # collected means a LOSS.
                #
                # Written once for debits, this suppressed the stop on exactly
                # the credit spreads that most needed it -- a structure at
                # maximum loss reads intrinsic far above its entry and would
                # have looked profitable at expiry. Found alongside the
                # inversion in _decompose on 2026-09-03; same bug class, two
                # places, and both only reachable on credit structures.
                #
                # Strictly compared: at exactly the entry there is nothing to
                # protect either way.
                intrinsic_ok = (parts_iv[0] < abs(st["entry"]) if st["credit"]
                                else parts_iv[0] > abs(st["entry"]))

            # Would selling right now, at the mark, realise a profit WORTH
            # TAKING? See STALL_MUST_BOOK_A_GAIN and STALL_MIN_GAIN_PCT.
            #
            # Computed here rather than read off ret_pct because the credit
            # sign convention is hand-rolled in this module and a silent
            # inversion on credit structures is the exact bug class that hit
            # _decompose and intrinsic_ok on 2026-09-03 -- same idea, two
            # places, both reachable only on credits.
            _gain_abs = ((abs(st["entry"]) - value) if st["credit"]
                         else (value - abs(st["entry"])))
            _gain_pct = (_gain_abs / entry_abs * 100.0) if entry_abs else 0.0
            books_a_gain = (not STALL_MUST_BOOK_A_GAIN) or (
                _gain_abs > 0 and _gain_pct >= STALL_MIN_GAIN_PCT)

            # Computed once per structure so the log line below can say the
            # rules are waiting rather than just omitting them.
            hold_until = (ORPHAN_HOLD_UNTIL if zero_dte
                          else (ORPHAN_LATER_HOLD_UNTIL or ORPHAN_HOLD_UNTIL))
            past_hold = _past_hold_until(hold_until)

            # A RESTING ASK OF OUR OWN. Read its state before anything decides,
            # so in_flight can tell our order from a stranger's and a fill is
            # booked before any rule tries to sell the same contracts again.
            own_ask_working = False
            _ask = rec.get("ask") if isinstance(rec.get("ask"), dict) else None
            if _ask:
                try:
                    _ast = (tradier_orders.order_status(str(_ask["id"])).get("status") or "").lower()
                except Exception:
                    _ast = "unknown"
                if _ast == "filled":
                    got = _fill_value(_ask["id"])
                    rec["ask"] = None
                    if got:
                        filled, filled_qty = got
                        real_pct = ((filled - entry_abs) / entry_abs * 100.0) if entry_abs else 0.0
                        logger.info(
                            "ORPHAN ASK FILLED: %s %g/%g x%d at %.2f (asked %.2f, entry %.2f, "
                            "%+.1f%%) — booking.", st["root"], st["long_strike"],
                            st["short_strike"], filled_qty, filled, _ask["price"],
                            entry_abs, real_pct)
                        _book(st, filled, real_pct, "ASK", qty=filled_qty)
                        if filled_qty >= st["qty"]:
                            peaks.pop(key, None)
                            state["structures"].pop(key, None)
                        else:
                            rec_s = state["structures"].get(key)
                            if rec_s:
                                rec_s["qty"] = st["qty"] - filled_qty
                    else:
                        logger.error("ORPHAN ASK: order %s reports filled but the fill could not "
                                     "be read — not booking; the next pass sees what remains.",
                                     _ask["id"])
                    continue
                if _ast in _DEAD:
                    logger.info("ORPHAN ASK: %s %g/%g order %s at %.2f came back %s — the ask "
                                "is off; the ladder resumes.", st["root"], st["long_strike"],
                                st["short_strike"], _ask["id"], _ask["price"], _ast)
                    rec["ask"] = None
                else:
                    own_ask_working = True

            # THE GIVEBACK, AND ITS CONFIRMATION CLOCK.
            #
            # gb_since is stamped when the threshold is first crossed and
            # CLEARED the moment it is not, so the clock measures a continuous
            # stretch rather than a total. A position that dips, recovers and
            # dips again starts over -- which is the whole point, since that
            # pattern is a wobble and not a reversal.
            gb_width = abs(st["short_strike"] - st["long_strike"])
            gb_need = gb_width * ORPHAN_INTRINSIC_GIVEBACK_PCT / 100.0
            gb_now = False
            if (ORPHAN_INTRINSIC_GIVEBACK_PCT > 0 and peak_iv is not None
                    and iv_now is not None
                    and (peak_iv > entry_abs if not st["credit"] else peak_iv < entry_abs)):
                given = ((peak_iv - iv_now) if not st["credit"]
                         else (iv_now - peak_iv))
                gb_now = given >= gb_need
            if gb_now:
                rec.setdefault("gb_since", now.isoformat())
            else:
                rec.pop("gb_since", None)
            gb_held_min = 0.0
            if rec.get("gb_since"):
                gb_held_min = (now - datetime.fromisoformat(
                    rec["gb_since"])).total_seconds() / 60.0
            giveback_held = gb_now and gb_held_min >= ORPHAN_GIVEBACK_CONFIRM_MIN
            if gb_now and not giveback_held:
                logger.info(
                    "ORPHAN %s %g/%g has given back %.2f of intrinsic (needs %.2f) "
                    "but only for %.1f min of the %.0f required — waiting to see if it "
                    "is a reversal or a wobble.",
                    st["root"], st["long_strike"], st["short_strike"],
                    ((peak_iv - iv_now) if not st["credit"] else (iv_now - peak_iv))
                    if (peak_iv is not None and iv_now is not None) else 0.0,
                    gb_need, gb_held_min, ORPHAN_GIVEBACK_CONFIRM_MIN,
                )

            # THE SLOW STOP'S CLOCK. Stamped when the mark first sits at or
            # below the level, cleared the moment it recovers, so the timer
            # measures one CONTINUOUS stretch rather than a total across the
            # session. A position that dips, recovers and dips again starts
            # over -- which is the point, since that pattern is chop.
            # The fast stop's own confirmation clock. Reset both when the mark
            # recovers above the level and when the intrinsic guard suppresses
            # the stop, so neither state accumulates time toward a close.
            stop_confirmed = True
            if ORPHAN_STOP_CONFIRM_MINUTES > 0 and zero_dte:
                if ret_pct <= stop_pct and not intrinsic_ok:
                    rec.setdefault("stop_since", now.isoformat())
                    held = (now - datetime.fromisoformat(
                        rec["stop_since"])).total_seconds() / 60.0
                    stop_confirmed = held >= ORPHAN_STOP_CONFIRM_MINUTES
                    if not stop_confirmed:
                        logger.info(
                            "ORPHAN %s %g/%g is %+.1f%%, past the %+.0f%% stop, "
                            "but only for %.1f of the %.0f minutes needed to confirm.",
                            st["root"], st["long_strike"], st["short_strike"],
                            ret_pct, stop_pct, held, ORPHAN_STOP_CONFIRM_MINUTES,
                        )
                else:
                    rec.pop("stop_since", None)
            else:
                rec.pop("stop_since", None)

            # The later-expiry stop. Guards live in the condition rather
            # than the branch so the reason line below stays a plain elif.
            later_stop_held = False
            LP = later_params(st)      # the LATER numbers for THIS expiry -- section 199
            if (LP["stop_pct"] < 0 and not zero_dte and past_hold
                    and not st["credit"] and not intrinsic_ok):
                if ret_pct <= LP["stop_pct"]:
                    rec.setdefault("later_stop_since", now.isoformat())
                    _lheld = (now - datetime.fromisoformat(
                        rec["later_stop_since"])).total_seconds() / 60.0
                    later_stop_held = _lheld >= LP["stop_minutes"]
                    if not later_stop_held:
                        logger.info(
                            "ORPHAN %s %g/%g has been %+.1f%% for %.0f of the %.1f "
                            "minutes the later-expiry stop needs (%d session(s) left, "
                            "stop %+.1f%%) — watching. It expires %s.",
                            st["root"], st["long_strike"], st["short_strike"],
                            ret_pct, _lheld, LP["stop_minutes"], LP["dte"],
                            LP["stop_pct"], st.get("expiry"),
                        )
                else:
                    rec.pop("later_stop_since", None)
            else:
                rec.pop("later_stop_since", None)

            # THE UNDERLYING STOP's clock. See ORPHAN_OTM_STOP. Cleared the
            # moment intrinsic returns, so it measures a continuous stretch
            # out of the money rather than a total.
            otm_held = False
            if (ORPHAN_OTM_STOP and not st["credit"] and past_hold
                    and parts_iv is not None):
                if parts_iv[0] <= ORPHAN_OTM_STOP_FLOOR:
                    rec.setdefault("otm_since", now.isoformat())
                    _oheld = (now - datetime.fromisoformat(
                        rec["otm_since"])).total_seconds() / 60.0
                    otm_held = _oheld >= ORPHAN_OTM_STOP_MINUTES
                else:
                    rec.pop("otm_since", None)
            else:
                rec.pop("otm_since", None)

            slow_held = False
            if ORPHAN_SLOW_STOP_PCT < 0 and zero_dte and past_hold:
                if ret_pct <= ORPHAN_SLOW_STOP_PCT:
                    rec.setdefault("slow_since", now.isoformat())
                    held = (now - datetime.fromisoformat(
                        rec["slow_since"])).total_seconds() / 60.0
                    slow_held = held >= ORPHAN_SLOW_STOP_MINUTES
                    if not slow_held:
                        logger.info(
                            "ORPHAN %s %g/%g has been %+.1f%% for %.0f of the %.0f "
                            "minutes the slow stop needs — watching.",
                            st["root"], st["long_strike"], st["short_strike"],
                            ret_pct, held, ORPHAN_SLOW_STOP_MINUTES,
                        )
                else:
                    rec.pop("slow_since", None)
            else:
                rec.pop("slow_since", None)

            # THE SHORT-STRIKE GUARD's clock. See ORPHAN_STRIKE_GUARD.
            strike_held = False
            if ORPHAN_STRIKE_GUARD and zero_dte and past_hold and not st["credit"]:
                sread = _session_tape(st["root"])
                beyond = False
                if sread is not None:
                    spot_s = sread[0]
                    k = float(st["short_strike"])
                    # ARMED ONLY ONCE IT HAS BEEN PINNED. The guard is about a
                    # spread SLIPPING OFF full value; one bought with the
                    # underlying already short of its short strike never had
                    # it, and at 11:13 the unarmed rule was three minutes from
                    # selling MU 1070/1080 bought at 1078 (short 1080) for
                    # being "through" a strike it had never crossed. Until
                    # armed, the stop's break-even line covers the slide.
                    if (spot_s > k) if st["right"] == "C" else (spot_s < k):
                        if not rec.get("strike_armed"):
                            logger.info("ORPHAN %s %g/%g: %s %.2f beyond the %g short strike "
                                        "— strike guard armed.", st["root"], st["long_strike"],
                                        st["short_strike"], st["root"], spot_s, st["short_strike"])
                        rec["strike_armed"] = True
                    breach = ((spot_s < k - ORPHAN_STRIKE_GUARD_BUFFER) if st["right"] == "C"
                              else (spot_s > k + ORPHAN_STRIKE_GUARD_BUFFER))
                    beyond = breach and bool(rec.get("strike_armed"))
                if beyond and _tape_against(st["right"], *sread):
                    rec.setdefault("strike_since", now.isoformat())
                    _skheld = (now - datetime.fromisoformat(
                        rec["strike_since"])).total_seconds() / 60.0
                    strike_held = _skheld >= ORPHAN_STRIKE_GUARD_MINUTES
                    if not strike_held:
                        logger.info(
                            "ORPHAN %s %g/%g: %s %.2f through the %g short strike under a "
                            "%s VWAP %.2f for %.0f of %.0f minutes — strike guard watching.",
                            st["root"], st["long_strike"], st["short_strike"], st["root"],
                            sread[0], st["short_strike"],
                            "falling" if st["right"] == "C" else "rising", sread[1],
                            _skheld, ORPHAN_STRIKE_GUARD_MINUTES)
                elif rec.pop("strike_since", None):
                    logger.info("ORPHAN %s %g/%g: strike guard clock reset (%s).",
                                st["root"], st["long_strike"], st["short_strike"],
                                "no tape reading" if sread is None else
                                "%s %.2f back on the right side of %g, or VWAP %.2f not against it"
                                % (st["root"], sread[0], st["short_strike"], sread[1]))
            else:
                rec.pop("strike_since", None)

            # THE UNDERLYING STOP's clock. See ORPHAN_UNDER_STOP.
            under_held = False
            if ORPHAN_UNDER_STOP and zero_dte and past_hold and not st["credit"] and entry_abs:
                uread = _session_tape(st["root"])
                line = under_stop_line(st["right"], float(st["long_strike"]), entry_abs,
                                       ORPHAN_UNDER_STOP_CUSHION)
                below = (uread is not None and
                         ((uread[0] < line) if st["right"] == "C" else (uread[0] > line)))
                tape_ok = (not ORPHAN_UNDER_STOP_REQUIRE_TAPE) or (
                    uread is not None and _tape_against(st["right"], *uread))
                if below and tape_ok:
                    rec.setdefault("under_since", now.isoformat())
                    _uheld = (now - datetime.fromisoformat(
                        rec["under_since"])).total_seconds() / 60.0
                    under_held = _uheld >= ORPHAN_UNDER_STOP_MINUTES
                    if not under_held:
                        logger.info(
                            "ORPHAN %s %g/%g: %s %.2f past the %.2f break-even line under an "
                            "adverse VWAP %.2f for %.0f of %.0f minutes — underlying stop watching.",
                            st["root"], st["long_strike"], st["short_strike"], st["root"],
                            uread[0], line, uread[1], _uheld, ORPHAN_UNDER_STOP_MINUTES)
                elif rec.pop("under_since", None):
                    logger.info("ORPHAN %s %g/%g: underlying stop clock reset (%s).",
                                st["root"], st["long_strike"], st["short_strike"],
                                "no tape reading" if uread is None else
                                "%s %.2f vs the %.2f line, VWAP %.2f" % (
                                    st["root"], uread[0], line, uread[1]))
            else:
                rec.pop("under_since", None)

            # THE TAPE EXIT's clock. See ORPHAN_TAPE_EXIT. One continuous
            # stretch of the underlying on the wrong side of a VWAP moving
            # against the structure; any minute that is not that -- including
            # a minute with no reading -- starts it over.
            tape_held = False
            tape_read = None
            if (ORPHAN_TAPE_EXIT and TAPE_EXIT_RESPECTS_INTRINSIC and intrinsic_ok
                    and zero_dte and not st["credit"]):
                # See TAPE_EXIT_RESPECTS_INTRINSIC: it pays at expiry, so the
                # tape is not a reason to sell it at a time-value discount.
                if rec.pop("tape_since", None):
                    logger.info(
                        "ORPHAN %s %g/%g: tape clock reset — intrinsic %.2f is above the "
                        "%.2f entry, so the tape exit does not apply while it pays at expiry.",
                        st["root"], st["long_strike"], st["short_strike"],
                        parts_iv[0], abs(st["entry"]))
            elif (ORPHAN_TAPE_EXIT and zero_dte and past_hold and not st["credit"]
                    and (value < abs(st["entry"]) or not ORPHAN_TAPE_EXIT_LOSERS_ONLY)):
                tape_read = _session_tape(st["root"])
                if tape_read is not None and _tape_against(st["right"], *tape_read):
                    rec.setdefault("tape_since", now.isoformat())
                    _tpheld = (now - datetime.fromisoformat(
                        rec["tape_since"])).total_seconds() / 60.0
                    tape_held = _tpheld >= ORPHAN_TAPE_EXIT_MINUTES
                    if not tape_held:
                        logger.info(
                            "ORPHAN %s %g/%g: %s %.2f %s a %s VWAP %.2f (was %.2f) for "
                            "%.0f of the %.0f minutes the tape exit needs — watching.",
                            st["root"], st["long_strike"], st["short_strike"],
                            st["root"], tape_read[0],
                            "under" if st["right"] == "C" else "over",
                            "falling" if st["right"] == "C" else "rising",
                            tape_read[1], tape_read[2], _tpheld, ORPHAN_TAPE_EXIT_MINUTES,
                        )
                else:
                    rec.pop("tape_since", None)
            else:
                rec.pop("tape_since", None)

            # A LATER EXPIRY'S ONLY RULE. Checked before everything else and
            # scoped to positions that are NOT expiring today, so it cannot
            # interfere with the 0DTE ladder.
            later_target_hit = False
            if (ORPHAN_LATER_TARGET_PCT > 0 and not expires_today):
                width = abs(st["short_strike"] - st["long_strike"])
                if width > 0:
                    # See LATER_TARGET_ON_INTRINSIC. Falls back to the mark
                    # whenever intrinsic cannot be computed, which is the old
                    # behaviour and never worse than having no target at all.
                    basis = (parts_iv[0] if (LATER_TARGET_ON_INTRINSIC
                                             and parts_iv) else value)
                    # For a credit structure the profit is the cost to close
                    # FALLING, so the target is the mirror of the debit case.
                    later_target_hit = (
                        basis <= width * (1.0 - ORPHAN_LATER_TARGET_PCT)
                        if st["credit"] else
                        basis >= width * ORPHAN_LATER_TARGET_PCT)

            # HOW MUCH INTRINSIC CLOSING RIGHT NOW WOULD THROW AWAY.
            # See ORPHAN_MAX_DRAG_WIDTH.
            _w = abs(st["short_strike"] - st["long_strike"])
            drag = (parts_iv[0] - value) if parts_iv else None
            drag_blocks = (
                ORPHAN_MAX_DRAG_WIDTH > 0
                and drag is not None and _w > 0
                and drag > _w * ORPHAN_MAX_DRAG_WIDTH)
            # See ORPHAN_DRAG_RELEASE_WIDTH. Intrinsic off its high by this
            # much means holding no longer recovers the drag, so the guard
            # stops protecting and starts trapping.
            if (drag_blocks and ORPHAN_DRAG_RELEASE_WIDTH > 0
                    and peak_iv is not None and iv_now is not None
                    and (peak_iv - iv_now) > _w * ORPHAN_DRAG_RELEASE_WIDTH):
                logger.info(
                    "ORPHAN %s %g/%g: intrinsic %.2f is %.2f off its %.2f peak "
                    "(past %.0f%% of the %g width) — the drag guard no longer "
                    "applies, holding does not recover it.",
                    st["root"], st["long_strike"], st["short_strike"],
                    iv_now, peak_iv - iv_now, peak_iv,
                    ORPHAN_DRAG_RELEASE_WIDTH * 100, _w,
                )
                drag_blocks = False

            stall_later_armed = (
                (not zero_dte) and past_hold and LP["stall_minutes"] > 0
                and rec["peak"] >= LP["stall_arm"]
                and quiet >= LP["stall_minutes"]
                and stall_pct <= rec["peak"] - _giveback_points(
                    st["root"], entry_abs, rec["peak"], None,
                    abs(st["short_strike"] - st["long_strike"]),
                    ORPHAN_LATER_STALL_GIVEBACK_BAND, LP["giveback_atr"]))

            stall_armed = (
                zero_dte and past_hold and STALL_MINUTES > 0 and rec["peak"] > 0
                and quiet >= STALL_MINUTES
                and stall_pct <= rec["peak"] - _giveback_points(
                    st["root"], entry_abs, rec["peak"], STALL_GIVEBACK_PCT,
                    abs(st["short_strike"] - st["long_strike"])))

            if (stall_armed or stall_later_armed) and not books_a_gain:
                if _gain_abs <= 0:
                    logger.info(
                        "ORPHAN %s %g/%g gave back to %+.1f%% from a "
                        "%+.1f%% peak, but the mark is %.2f against a %.2f "
                        "entry — closing books a LOSS of %+.1f%%, and the "
                        "stall does not realise losses. That is the stop's "
                        "job and it is intrinsic-aware.",
                        st["root"], st["long_strike"], st["short_strike"],
                        stall_pct, rec["peak"], value, entry_abs, _gain_pct,
                    )
                else:
                    logger.info(
                        "ORPHAN %s %g/%g gave back to %+.1f%% from a "
                        "%+.1f%% peak, but closing at %.2f books only "
                        "%+.1f%% — under the %.0f%% floor, so it is not worth "
                        "taking and the position runs on to the stop or the "
                        "flatten.",
                        st["root"], st["long_strike"], st["short_strike"],
                        stall_pct, rec["peak"], value, _gain_pct,
                        STALL_MIN_GAIN_PCT,
                    )
            stall_ready = stall_armed and books_a_gain
            stall_later_ready = stall_later_armed and books_a_gain

            if drag_blocks and (later_target_hit or stall_later_ready
                                or stall_ready):
                logger.info(
                    "ORPHAN %s %g/%g would close on %s at %.2f, but "
                    "intrinsic is %.2f — closing now forfeits %.2f, which is "
                    "%.0f%% of the %.0f width and above the %.0f%% ceiling. It "
                    "expires %s and extrinsic goes to zero by then, so it "
                    "holds.",
                    st["root"], st["long_strike"], st["short_strike"],
                    "LATER_TARGET" if later_target_hit
                    else ("STALL" if stall_ready else "STALL_LATER"),
                    value, parts_iv[0], drag, drag / _w * 100.0, _w,
                    ORPHAN_MAX_DRAG_WIDTH * 100.0,
                    st.get("expiry") or "today",
                )

            reason = None
            if later_target_hit and not drag_blocks and past_hold:
                reason = "LATER_TARGET"
            elif floor_breached:
                # Ahead of every other rule, and deliberately blind to expiry,
                # the hold window and the intrinsic guards. Those all decide
                # whether a POSITION is working; this one has already decided
                # the account is not.
                reason = "ACCOUNT_FLOOR"
            elif strike_held:
                # Ahead of the stop's intrinsic hold-off on purpose: that
                # branch is what let the 1075 -> 1071 slide go unanswered.
                reason = "STRIKE_GUARD"
            elif under_held:
                # Also ahead of the intrinsic hold-off: this IS the judgement
                # on the underlying that the hold-off was standing in for.
                reason = "UNDER_STOP"
            elif otm_held:
                # NO CONFIRMATION, deliberately. The mark stops wait to tell a
                # wick from a trend; this is not reading the mark at all, and
                # every delayed variant measured WORSE than having no rule.
                reason = "OTM_STOP"
            elif zero_dte and ret_pct <= stop_pct and not past_hold:
                # AHEAD OF THE INTRINSIC GUARD, because this holds for a
                # different reason. That guard declines to stop a position
                # that still pays at expiry; this one declines to trust a
                # -25% mark printed into the opening spread at all.
                logger.info(
                    "ORPHAN %s %g/%g is %+.1f%% on the mark but it is before "
                    "%s — holding through the opening spread rather than booking "
                    "a loss on it.",
                    st["root"], st["long_strike"], st["short_strike"], ret_pct,
                    ORPHAN_HOLD_UNTIL,
                )
            elif zero_dte and ret_pct <= stop_pct and intrinsic_ok:
                logger.info(
                    "ORPHAN %s %g/%g is %+.1f%% on the mark but holds %.2f of "
                    "intrinsic against a %.2f entry — NOT stopping out a position "
                    "that pays at expiry. The gap is time premium on the short leg.",
                    st["root"], st["long_strike"], st["short_strike"], ret_pct,
                    parts_iv[0], abs(st["entry"]),
                )
            elif zero_dte and ret_pct <= stop_pct and stop_confirmed:
                reason = "STOP_LOSS"
            elif slow_held:
                # Below the fast stop's level in the ladder: a grind is less
                # urgent than a gap, and if the fast stop also applies it
                # should be the one that names the exit.
                reason = "SLOW_STOP"
            elif tape_held:
                # The underlying has spent ORPHAN_TAPE_EXIT_MINUTES on the wrong
                # side of a VWAP moving against a losing debit. Section 211:
                # +9,891 over the stop alone on 130 structure-days. Below the
                # stops so a genuine breakdown is still named by them.
                reason = "TAPE_EXIT"
            elif later_stop_held:
                # Only reachable when zero_dte is false, so it can never race
                # the expiry-day ladder above.
                reason = "LATER_STOP"
            elif zero_dte and _past_force_close():
                # Time beats everything. These settle in shares, not cash.
                reason = "FORCE_CLOSE"
            elif (ORPHAN_TARGET_RETURN_PCT > 0
                  and ret_pct >= ORPHAN_TARGET_RETURN_PCT
                  and not drag_blocks and past_hold):
                # Return on cost, not a fraction of max profit. See the knob.
                #
                # DRAG-GATED SINCE 2026-09-17, and it is the only rule in this
                # chain that was taking profit without being. A live SNDK
                # 1510/1550 weekly showed the whole failure in three log lines:
                #
                #   LATER_TARGET would close at 20.10, intrinsic 40.00 --
                #     forfeits 19.90, 50% of width, REFUSED by the guard
                #   LATER_TARGET would close at 26.70, intrinsic 40.00 --
                #     forfeits 13.30, 33% of width, REFUSED again
                #   TARGET fired at 26.70 (+54.8%) and sold it
                #
                # The guard turned the same exit away twice and this branch let
                # it through the side door, on a spread sitting at its MAXIMUM
                # intrinsic. Booked +2,348 where holding to expiry was worth
                # about +4,550.
                #
                # A take-profit is exactly the kind of rule the ceiling is for:
                # it is a judgement that the position is finished, and a
                # position cannot be finished while a third of its width is
                # still coming back.
                #
                # NOTE WHAT IS DELIBERATELY *NOT* ADDED HERE. This branch is
                # also ungated on zero_dte, so the 0DTE target applies to
                # weeklies alongside LATER_TARGET, and ungated on past_hold, so
                # it can fire into the opening spread -- it did, at 09:31.
                # Both are arguable and neither is measurable yet: the harness
                # cannot settle a weekly. Left as they are rather than changed
                # on the same single observation.
                reason = "TARGET"
            elif (ceiling is not None and ret_pct >= ceiling
                  and not drag_blocks and past_hold):
                # GATED WITH THE REST OF THE PROFIT BRANCHES, 2026-09-17, even
                # though ORPHAN_CEILING_FRACTION is 0 and this cannot fire.
                # It is the same shape as the TARGET bug in section 174 and it
                # was waiting for someone to turn a knob. "Every branch that
                # takes profit is drag-gated" has to be an invariant of the
                # chain, not a property of which settings happen to be zero.
                reason = "CEILING"
            elif zero_dte and past_hold and giveback_held and not drag_blocks:
                # See ORPHAN_INTRINSIC_GIVEBACK. No books_a_gain test here on
                # purpose -- this rule exists precisely for the case where the
                # mark is under water and the expiry value is walking away.
                reason = "GIVEBACK"
            elif stall_ready and not drag_blocks:
                # The take-profit ARMS this rather than firing it, exactly as
                # the engine's own credit window now does: a structure that
                # keeps making new highs is not finished.
                reason = "STALL"
            elif stall_later_ready and not drag_blocks:
                # Armed by a real profit, booked on a small giveback. See the
                # knobs above for why arming is what makes the tight giveback
                # safe on a position that has days left.
                reason = "STALL_LATER"
            # The expiry check has moved INTO the reason logic above, so the
            # ceiling can act on a later expiry while the 0DTE rules cannot.
            # NOT WHILE AN ORDER IS ALREADY WORKING ON THESE LEGS.
            #
            # Nothing used to check. A resting manual limit and the 15:45
            # flatten could both fill, selling the structure twice and leaving
            # a short leg naked into expiry. Hit live on 2026-09-04 with a
            # SNDK limit resting at 35.00 against a managed position; that day
            # it was worked around by an external watcher that cancelled the
            # limit five minutes before the flatten, which is not a mechanism
            # anyone should have to remember.
            #
            # The broker is the authority on what is working, so this asks it
            # rather than tracking submissions locally -- orders placed by
            # hand, from a phone, or by a previous run of this process all
            # count, and none of them would appear in local state.
            if own_ask_working and reason in _ASK_SUPPRESSED:
                logger.info(
                    "ORPHAN %s %g/%g: %s would sell at the %.2f bid, but a %.2f ask is "
                    "resting above it — the ask is that exit, asking more. Holding.",
                    st["root"], st["long_strike"], st["short_strike"], reason, value,
                    rec["ask"]["price"])
                reason = None
            # Our own resting ask is not a stranger's order: the ladder stays live.
            in_flight = bool(working & {st["long"], st["short"]}) and not own_ask_working
            if in_flight and reason:
                logger.info(
                    "ORPHAN %s %g/%g wants %s but an order is already working "
                    "on its legs — standing down rather than selling it twice.",
                    st["root"], st["long_strike"], st["short_strike"], reason,
                )
            elif in_flight:
                # SAY IT EVERY CYCLE, not only when a rule wants to act. On
                # 2026-09-23 a manual limit placed at 08:50 on MU 1070/1080
                # rested until 10:28 and the log said only "[observation
                # only]" -- nothing named the order as the reason the stop,
                # the ask and the flatten were all off.
                logger.warning(
                    "ORPHAN %s %g/%g: an order this engine did not place is working "
                    "on its legs — stop, stall, ask and the 15:45 flatten are OFF for "
                    "it until that order fills or is cancelled.",
                    st["root"], st["long_strike"], st["short_strike"],
                )
            manageable = (MANAGE_ORPHANS
                          and not in_flight
                          and (floor_breached or expires_today or later_target_hit
                               or not ORPHAN_ACT_EXPIRY_DAY_ONLY)
                          and (not st.get("inferred") or MANAGE_INFERRED)
                          and (not MANAGE_UNDERLYING or st["root"] in MANAGE_UNDERLYING)
                          and _quotes_tradeable(
                              tradier_orders.quotes([st["long"], st["short"]]),
                              st, reason))
            # SAY ONLY WHAT APPLIES TO THIS POSITION.
            #
            # The verdict used to print "stop -25%" on every line including
            # positions expiring later, where zero_dte gates the stop off and
            # it can never fire. The behaviour was right and the log lied about
            # it -- which is worse than a cosmetic problem here, because the
            # whole point of these lines is to let a human check that the rules
            # in force are the rules intended.
            if reason:
                verdict, verdict_scope = reason, ""
            else:
                parts = []
                if ceiling is not None:
                    parts.append("ceiling %+.0f%%" % ceiling)
                if zero_dte:
                    parts.append("stop %+.0f%%%s" % (
                        stop_pct, "" if past_hold else " from %s" % ORPHAN_HOLD_UNTIL))
                    if STALL_MINUTES > 0:
                        # The effective number, not the setting -- under the
                        # fraction basis it is derived per structure and the
                        # raw setting would describe a rule not in force.
                        parts.append("stall %.1fpts/%.0fmin%s" % (
                            _giveback_points(
                                st["root"], entry_abs, rec["peak"],
                                STALL_GIVEBACK_PCT,
                                abs(st["short_strike"] - st["long_strike"])),
                            STALL_MINUTES,
                            "" if past_hold else " from %s" % ORPHAN_HOLD_UNTIL))
                    if ORPHAN_FORCE_CLOSE:
                        parts.append("flatten %s" % ORPHAN_FORCE_CLOSE)
                    if isinstance(rec.get("ask"), dict):
                        parts.append("ASK %.2f resting, floor %.2f, %d step(s)" % (
                            rec["ask"]["price"], rec["ask"].get("floor", 0.0),
                            int(rec["ask"].get("steps", 0))))
                else:
                    # A LATER EXPIRY NOW HAS A STOP TOO, so the line has to say
                    # so. It printed only the stall, which was honest while a
                    # weekly had no downside rule and becomes a lie the moment
                    # one exists -- the same failure the zero_dte split above
                    # was written to fix.
                    if LP["stop_pct"] < 0 and not st["credit"]:
                        parts.append("stop %+.1f%%/%.0fmin%s" % (
                            LP["stop_pct"], LP["stop_minutes"],
                            "" if past_hold else " from %s" % hold_until))
                    if LP["stall_minutes"] > 0:
                        # REPORT THE EFFECTIVE GIVE-BACK, not the setting. Under
                        # the ATR configuration the number that actually applies
                        # is derived per structure, and a log line printing the
                        # raw setting would describe a rule the engine is not
                        # using -- exactly the class of quiet lie the verdict line
                        # exists to prevent.
                        # And the WEEKLY band, not the 0DTE one: without it
                        # this printed 16.0 pts on SNDK 1700/1780 while the
                        # rule in force was 64.2 (2026-09-18).
                        _gb = _giveback_points(
                            st["root"], entry_abs, rec["peak"], None,
                            abs(st["short_strike"] - st["long_strike"]),
                            ORPHAN_LATER_STALL_GIVEBACK_BAND, LP["giveback_atr"])
                        _atr_note = ""
                        if LP["giveback_atr"] > 0:
                            _a = _atr_for(st["root"])
                            if _a:
                                _atr_note = " =%.2f%s@%.2fATR" % (
                                    LP["giveback_atr"] * _a, st["root"], LP["giveback_atr"])
                        parts.append("stall %.1fpts%s/%.0fmin %s" % (
                            _gb, _atr_note, LP["stall_minutes"],
                            "ARMED" if rec["peak"] >= LP["stall_arm"]
                            else "arms +%.0f%%" % LP["stall_arm"]))
                    parts.append("%d session(s) left" % LP["dte"])
                verdict = "holding (%s)" % ", ".join(parts) if parts else "holding"
                verdict_scope = "" if zero_dte else "  [expires %s]" % st.get("expiry")
            iv_note = ""
            if parts_iv:
                intr, extr = parts_iv
                iv_note = "  [intrinsic %.2f, extrinsic %+.2f]" % (intr, extr)
            logger.info(
                "ORPHAN %s %s %g/%g x%d %s: entry %.2f value %.2f %+.1f%% "
                "(peak %+.1f%%, %.0f min ago) — %s%s",
                st["root"], st["right"], st["long_strike"], st["short_strike"],
                st["qty"], "credit" if st["credit"] else "debit",
                abs(st["entry"]), value, ret_pct, rec["peak"], quiet,
                verdict + verdict_scope + iv_note,
                ("  [INFERRED pairing — observation only]"
                 if st.get("inferred") and not MANAGE_INFERRED
                 else ("" if manageable else "  [observation only]")),
            )
            if reason and manageable:
                if own_ask_working and isinstance(rec.get("ask"), dict):
                    outcome = _ask_cancel(rec["ask"], st)
                    if outcome == "filled":
                        logger.info("ORPHAN %s %g/%g wanted %s, but the resting ask filled first "
                                    "— booking on the next pass.", st["root"],
                                    st["long_strike"], st["short_strike"], reason)
                        reported.append(st)
                        continue
                    if outcome == "unknown":
                        logger.warning("ORPHAN %s %g/%g wants %s but the ask's cancel is "
                                       "unconfirmed — standing down this pass rather than "
                                       "selling it twice.", st["root"], st["long_strike"],
                                       st["short_strike"], reason)
                        reported.append(st)
                        continue
                    logger.info("ORPHAN %s %g/%g: %s — withdrew the %.2f ask first.",
                                st["root"], st["long_strike"], st["short_strike"], reason,
                                rec["ask"]["price"])
                    rec["ask"] = None
                got = _close(st, reason, value)
                if got is not None:
                    filled, filled_qty = got
                    # Book what it FILLED at, not the mark that triggered it.
                    entry = abs(st["entry"])
                    real_pct = ((((entry - filled) if st["credit"] else (filled - entry))
                                 / entry * 100.0) if entry else ret_pct)
                    if abs(filled - value) > 0.005:
                        logger.info(
                            "ORPHAN fill %.2f against a %.2f mark (%+.1f%% not %+.1f%%) "
                            "— booking the fill.", filled, value, real_pct, ret_pct)
                    _book(st, filled, real_pct, reason, qty=filled_qty)
                    if filled_qty >= st["qty"]:
                        peaks.pop(key, None)
                        state["structures"].pop(key, None)
                    else:
                        # A PARTIAL close leaves a smaller structure open. Keep
                        # the peak, shrink the cache, and let the next pass see
                        # what is left rather than treating it as finished.
                        logger.warning(
                            "ORPHAN partial close: %d of %d %s %g/%g filled — "
                            "%d contracts remain.", filled_qty, st["qty"], st["root"],
                            st["long_strike"], st["short_strike"], st["qty"] - filled_qty)
                        rec_s = state["structures"].get(key)
                        if rec_s:
                            rec_s["qty"] = st["qty"] - filled_qty
            elif manageable and ORPHAN_ASK and (
                    own_ask_working
                    or (past_hold and parts_iv
                        and (zero_dte or not ORPHAN_ASK_ZERO_DTE_ONLY)
                        and parts_iv[0] >= abs(st["short_strike"] - st["long_strike"]) - 0.01)):
                # Nothing in the ladder wants to act. Pinned at full width, or
                # an ask already resting: place, hold, step or withdraw it.
                _ask_manage(st, rec, value, entry_abs,
                            abs(st["short_strike"] - st["long_strike"]), own_ask_working)
            reported.append(st)

        # A structure that has vanished since the last pass was closed by
        # someone -- the human, an assignment, an expiry. Book what we can and
        # stop tracking its peak.
        for gone in [k for k in peaks if k not in seen]:
            peaks.pop(gone, None)
        _save(state)
        return reported
    except Exception:
        logger.exception("Orphan review failed — continuing; this must not break a cycle.")
        return []
