"""Place the day's best 0DTE debit spreads, ranked by EV, sized to a budget.

    python scripts/dte0_trade.py                       # DRY RUN, prints only
    python scripts/dte0_trade.py --live                # places orders
    python scripts/dte0_trade.py --budget 1500 --max-trades 3

THIS ONE TRADES, which no other script in this repository does, so the guards
are listed before anything else.

    --live is required            dry run is the default and prints the same
                                  plan without sending it
    TRADING_DTE0_LIVE=true        must ALSO be set; --live alone does nothing
    TRADING_DTE0_MAX_BUDGET       hard ceiling, 1500, applied after sizing
    --max-trades                  3 by default, one per underlying
    --rotate                      re-entry pass. Needs TRADING_DTE0_ROTATE too,
                                  skips names held / in cooldown / at the cap,
                                  and refuses any new entry past 13:30
    already-held check            refuses a symbol the account already holds
                                  an option in for today's expiry, so a rerun
                                  cannot double a position
    MAX_ORDER_CONTRACTS           the same clamp every order passes through.
                                  Sizing respects it rather than discovering
                                  it: on 2026-09-11 an 18-lot exit became four
                                  fills a minute apart and cost $72 of slippage

WHAT IT IS BUYING, AND WHY ONLY DEBITS. Credit structures lock capital against
the full width and this book has no measured record selling premium; IV/RV on
2026-09-12 came back 0.48 to 0.83 across these names, which says implied is
BELOW realised and premium is cheap. Buying is what that reading supports.

RANKED BY EV FROM THE SCREENER, not from a second implementation. rank()
is what /screener/verticals and weekly_pick.py both call, so a trade placed
here and a row printed there cannot diverge.

AND FILTERED BY THE THREE CONSTRAINTS, each of which cost money to learn:

    entry 30-65% of width   above it the +30% target is unreachable, since
                            max return is (width-entry)/entry and 0.77 of
                            width IS 30%. Below it the premium is mostly time
                            value and the -10% stop sits nearer than one
                            morning of theta.
    extrinsic under 25%     the same failure in the units that cause it
    target within 0.3 ATR   the move to +30% must be an ordinary session

The screener ranks by EV and knows nothing about any of that -- it happily
returned NVDA 200/220 at +28 points of edge with 35% of its premium in time
value. EV picks the best of what is sound; the constraints decide what is
sound.

EXITS ARE NOT THIS SCRIPT'S JOB. Whatever it opens is picked up by
trading_engine/orphans.py on the next cycle and managed by the same ladder as
every manual position: -10% stop (intrinsic-guarded), +30% target, a trail at
15% of the profit band, and the 15:45 flatten. Nothing here needs to know
that, and nothing here should duplicate it.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date, datetime, time as dtime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import psycopg2  # noqa: E402

from trading_engine import tradier_orders  # noqa: E402
from trading_engine.macro_calendar import (  # noqa: E402
    blackout_active as event_blackout_active, describe as describe_event)
from trading_engine.data_feed import fetch_option_chain, fetch_spot  # noqa: E402
from trading_engine.screener import rank  # noqa: E402
from trading_engine import vwap_gate  # noqa: E402
from trading_engine import weekly_vwap_gate  # noqa: E402
from trading_engine import options_flow  # noqa: E402
from trading_engine.symbol_news import (classify_day,  # noqa: E402
                                        verdict_at)

logger = logging.getLogger("dte0_trade")
NY = ZoneInfo("America/New_York")

LIVE_ENABLED = os.getenv("TRADING_DTE0_LIVE", "false").lower() == "true"
MAX_BUDGET = float(os.getenv("TRADING_DTE0_MAX_BUDGET", "1500"))
# THE WEEKLY BOOK RUNS THROUGH THIS SAME SCRIPT -- section 198. `--book weekly`
# changes four things and nothing else: the expiry resolves to a Friday
# instead of today, the budget ceiling is its own knob, a name the account
# holds on ANY expiry is refused (a weekly on top of a 0DTE in the same name
# is the same bet twice), and the closing log names the ladder that will
# manage it (the LATER rules, not the 15:45 flatten). Every gate -- macro,
# news, tape, options flow, EV/Pwin/edge, structure limits, rotation cooldown
# -- is the one the 0DTE book has been measured under. The ranker's Pwin
# already scales with days to expiry, so the structure constraints hold.
WEEKLY_MAX_BUDGET = float(os.getenv("TRADING_WEEKLY_MAX_BUDGET", "5000"))
WEEKLY_MIN_DAYS = int(os.getenv("TRADING_WEEKLY_MIN_DAYS", "2"))
# THE STRUCTURE BAND IS A 0DTE BAND. "Entry 30-75% of width, short strike
# within 0.40 ATR" was measured for a spread that has to finish TODAY. Over
# five sessions the same 0.40 ATR is a fifth of the expected move
# (0.40 / sqrt(6) sessions), and the first weekly dry run on 2026-09-19
# rejected all 48 candidates on "entry 9-26% of width". The weekly book gets
# its own band -- the plan's moderate geometry, R:R 1..3 ranked by edge --
# and a short-strike allowance scaled for the horizon. Unmeasured, stated.
WEEKLY_MIN_EW = float(os.getenv("TRADING_WEEKLY_MIN_ENTRY_WIDTH", "0.20"))
WEEKLY_MAX_EW = float(os.getenv("TRADING_WEEKLY_MAX_ENTRY_WIDTH", "0.75"))
WEEKLY_MAX_SHORT_ATR = float(os.getenv("TRADING_WEEKLY_MAX_SHORT_ATR", "9.0"))
WEEKLY_RR = (float(os.getenv("TRADING_WEEKLY_RR_MIN", "1.0")),
             float(os.getenv("TRADING_WEEKLY_RR_MAX", "3.0")))
# Two more 0DTE numbers that cannot survive a week: "extrinsic <= 25% of
# premium" (a five-day spread IS mostly time value -- the second dry run
# rejected 52 of 52 on it) and "target within N ATR" (the ATR is a daily
# figure and the target has five sessions to get there).
# Third dry run: with 80% extrinsic and 1.0 ATR the board rejected 52 of 52
# again -- 32 on "extrinsic 97-100%" (a slightly-OTM weekly IS all time value
# until it is not) and 20 on "short strike 1.9-3.1 ATR out" (a DAILY ATR; six
# sessions expect about 2.4 of them). So the weekly book does not use those
# two limits at all. Its guard against the lottery ticket is the plan's:
# entry >= 20% of width, R:R 1..3 by edge, and Pwin >= WEEKLY_MIN_PWIN. The
# ranker's Pwin already carries the horizon.
WEEKLY_MAX_EXTRINSIC = float(os.getenv("TRADING_WEEKLY_MAX_EXTRINSIC", "100.0"))
WEEKLY_MAX_TARGET_ATR = float(os.getenv("TRADING_WEEKLY_MAX_TARGET_ATR", "9.0"))
WEEKLY_MIN_PWIN = float(os.getenv("TRADING_WEEKLY_MIN_PWIN", "0.45"))
BOOK = "dte0"           # set by --book; read by _passes
# QQQ IS DELIBERATELY ABSENT. The engine trades QQQ 0DTE itself from 09:45
# through its own playbook, and a second QQQ position placed here would be an
# independent bet on the same underlying, sized separately, with the engine
# logging a RECONCILE error every minute because it tracks its position in its
# own database rather than from the broker. Duplicating the one instrument the
# engine already covers is the opposite of diversifying into single names.
# Every name with Monday and Wednesday expiries, checked against the chain on
# 2026-09-12. QQQ is excluded above; SNDK and CRWV list Fridays only.
# MU added 2026-09-14. Every 0DTE name the engine manages should also be a name
# it can OPEN, otherwise the book can only inherit a position in that symbol and
# never choose one -- which is how MU came to be held, managed and ungraded at
# the same time. Keep this in step with TRADING_HOURLY_SYMBOLS: a name traded
# here with no news row cannot be vetoed by the news gate.
#
# Both directions, already: rank() is called for "call" and "put" with
# structure="debit", so each name is considered as a call debit spread AND a
# put debit spread and the better EV wins. Nothing here is long-only.
SYMBOLS = os.getenv("TRADING_DTE0_TRADE_SYMBOLS",
                    "NVDA,TSLA,AAPL,AMZN,MSFT,META,GOOGL,AVGO,MU")

# A WIDER UNIVERSE NEEDS THE LIQUIDITY GATE THAT dte0_shadow ALREADY HAS.
#
# Median near-ATM quote as a share of mid, Monday's chain:
#
#     NVDA 2.6%  MU 2.8%  QQQ 3.9%  META 6.3%
#     AMZN 11.9%  GOOGL 13.3%  MSFT 17.0%  AVGO 21.2%
#
# A vertical crosses that twice, on two legs, in and out. Against a structure
# whose maximum return is 30-50%, AVGO's quote eats the trade before direction
# matters. EV penalises a wide quote indirectly -- the screener prices at
# natural, so a bad chain raises `cost` and the break-even with it -- but
# indirectly is not the same as refused, and a high enough EV would still let
# it through.
MAX_QUOTE_PCT = float(os.getenv("TRADING_DTE0_MAX_QUOTE_PCT", "15.0"))

# ROTATION: re-enter a name after it exits, on fresh news.
#
# The engine has had this for QQQ since long before tonight, and its two
# numbers were tuned on measured outcomes -- TRADING_WIN_COOLDOWN_MINUTES=30
# and TRADING_REENTRY_COOLDOWN_MINUTES=90. This borrows the shape rather than
# inventing one.
#
# WHAT IT COSTS, because it is not free and compounds:
#
#     NVDA quote 2.6% of mid  ->  round trip ~5.2% of premium
#     $374 position           ->  ~$19 a rotation
#     +30% target             ->  ~$112
#     three rotations         ->  ~$57 of spread against $336 of targets
#
# About 17% of each target spent getting in and out, and the later entries are
# structurally worse: extrinsic has decayed, entry/width drifts to the bottom
# of the band, and a 14:30 entry has 75 minutes before the 15:45 flatten.
# Hence the cutoff and the per-day cap.
#
# OFF BY DEFAULT AND SEPARATE FROM --live. This is the first order-placing
# path here without a track record; everything else that trades by itself is
# QQQ-only and measured. dte0_shadow is building the paper record in parallel.
ROTATE_ENABLED = os.getenv("TRADING_DTE0_ROTATE", "false").lower() == "true"
ROTATE_COOLDOWN_MIN = float(os.getenv("TRADING_DTE0_ROTATE_COOLDOWN_MIN", "30"))
MAX_ROTATIONS = int(os.getenv("TRADING_DTE0_MAX_ROTATIONS", "3"))
# No NEW entry after this. A position opened late cannot reach a +30% target
# before the flatten takes it at whatever the mark is.
ROTATE_CUTOFF = os.getenv("TRADING_DTE0_ROTATE_CUTOFF", "13:30")
# THE WEEKLY BOOK HAS ITS OWN CUTOFF (section 237). The reason above is a 0DTE
# reason -- a same-day spread opened late cannot reach its target before the
# flatten -- and it does not hold for a spread held for days. Sharing it meant
# the weekly book's 13:50 ET run logged "past the rotation cutoff" and entered
# nothing on every day it ran (09-22 and 09-23 at the 11:30 cutoff then).
WEEKLY_ROTATE_CUTOFF = os.getenv("TRADING_WEEKLY_ROTATE_CUTOFF", "15:30")
MIN_EW = float(os.getenv("TRADING_PICK_MIN_ENTRY_WIDTH", "0.30"))
# 0.75, NOT 0.65. The boundary is arithmetic: max return is (width-entry)/entry,
# so a +30% target becomes unreachable at exactly e/w = 1/1.30 = 0.769. 0.65 was
# margin chosen by feel, and backtested against 2026-09-11 it cost most of its
# own benefit -- it rejected the three losing QQQ trades (e/w 0.814, 0.800,
# 0.757, max returns 23%, 25%, 32%) AND the +$355 winner at 0.68 whose max
# return was 47%. At 0.75 the same filter drops exactly the three losers:
#
#     as traded        +1,256.02
#     band 0.30-0.65   +1,286.00   +30    three losers and one winner gone
#     band 0.30-0.75   +1,641.02   +385   three losers gone, winners intact
MAX_EW = float(os.getenv("TRADING_PICK_MAX_ENTRY_WIDTH", "0.75"))
MAX_EXTRINSIC = float(os.getenv("TRADING_PICK_MAX_EXTRINSIC", "25.0"))
MAX_TARGET_ATR = float(os.getenv("TRADING_PICK_MAX_TARGET_ATR", "0.30"))

# HOW FAR OUT THE SHORT LEG MAY SIT, IN ATR.
#
# The fourth constraint, and the one EV is blindest to. Ranking on EV prefers
# a wider structure because the paper reward is bigger, without noticing that
# the strike it sold is unreachable and therefore worthless:
#
#     NVDA 218.29, ATR 7.67
#       215 call  ask 3.85  delta 0.77   bought
#       225 call  bid 0.11  delta 0.07   sold -- 6.71 out = 0.87 ATR
#
# Eleven cents against a $385 long call. That is 2.9% of the cost, in exchange
# for capping every gain above 225. Move the short in to 222.5 and it earns
# 0.30; to 220 and it earns 0.84, more than a fifth of the premium.
#
# The number comes from what these names can actually travel in one session:
#
#     NVDA   3-4   of ATR 7.67   = 0.46 ATR
#     META   5-10  of ATR 21.32  = 0.35 ATR
#     MU    10-15  of ATR 44.15  = 0.28 ATR
#
# A short strike beyond that is not a short leg, it is a decoration that costs
# upside. 0.40 sits in the middle of the three.
MAX_SHORT_ATR = float(os.getenv("TRADING_PICK_MAX_SHORT_ATR", "0.40"))
# THE SHORT LEG MUST PAY SOMETHING (2026-09-23, section 220). Both books.
#
# The weekly book dropped the short-strike ATR limit (a daily ATR is the wrong
# yardstick over five sessions), and nothing replaced it on PRICE: at 09:50 it
# bought TSLA 372.5/417.5 x1, long filled 13.53, short 417.5 sold for 0.20 --
# 1.5% of the long, 2.49 ATR out. That is a long call in a spread's clothes:
# the short caps nothing it will reach, and every width-based exit (90% of a
# 45 width is 40.50) is unreachable. Distance was the wrong test anyway; what
# matters is what the short leg PAYS. Its bid must be at least this percent of
# the long leg's ask. Operator decision, unmeasured; 0 disables.
MIN_SHORT_PAYS_PCT = float(os.getenv("TRADING_PICK_MIN_SHORT_PAYS_PCT", "10") or 0)
TARGET_PCT = float(os.getenv("TRADING_ORPHAN_TARGET_RETURN_PCT", "30.0"))

# THE MORNING'S NEWS READ, AS A VETO ON DIRECTION.
#
# The same shape as the engine's TRADING_NEWS_DIRECTION gate: the verdict can
# REFUSE a structure that contradicts it and can never propose one. A trade
# still has to clear EV, edge and all three structure constraints first; news
# only removes.
#
# The screener carries its own conflict guard but it fires on VERY_BEARISH and
# VERY_BULLISH alone, which are rare -- 6 of 204 graded verdicts. The plain
# readings are 44 more, and if the read is worth consulting at all it is worth
# consulting at BEARISH, so this widens it to match the engine rather than
# leaving two different definitions of "contradicts" in one system.
#
# The verdict is written at 09:30 by news_watch and this runs at 09:45, so it
# is the morning's news and not yesterday's -- session_headlines windows from
# the previous close and the novelty filter drops re-reported stories.
NEWS_VETO = os.getenv("TRADING_DTE0_NEWS_VETO", "true").lower() == "true"
# A FLOOR SUITED TO THE POLYGON SCALE, which is its own instrument.
#
# This shared TRADING_NEWS_DIRECTION_MIN_CONF (0.70) on the principle that two
# gates reading ONE signal must not disagree. That principle still holds, and
# it no longer applies: the engine's gate now reads the OBJECTIVE macro score
# (crude/10Y/VIX, floor 0.25) and this one reads Polygon ticker sentiment.
# Different signals, different scales, different floors -- sharing a number
# across them was the mistake, not the fix.
#
# AND 0.70 IS UNREACHABLE ON THE CORRECTED SCALE. Polygon scores are now shrunk
# by sample size, so a lone article cannot print 1.00. Across all 92 rows
# recorded: 12 cleared 0.70 raw, ZERO clear it shrunk, 3 clear 0.50 and 5 clear
# 0.40. Keeping 0.70 would leave the veto as dead code that reads as an armed
# guard.
#
# 0.50 fires on roughly 3% of readings -- rare, which is what a veto on an
# unmeasured signal should be, and reachable, which it was not.
NEWS_MIN_CONF = float(os.getenv("TRADING_DTE0_NEWS_MIN_CONF", "0.50"))
NEWS_BEARISH = {"BEARISH", "VERY_BEARISH"}
NEWS_BULLISH = {"BULLISH", "VERY_BULLISH"}

# THE TAPE, AS A VETO ON DIRECTION -- section 194. After macro and news have
# had their say, the flow gate asks whether the session is actually being
# BOUGHT before a call debit goes on: spot above the running VWAP, VWAP higher
# than 30 minutes ago, most bars closing above it, volume arriving on closes
# near bar highs. Puts need the mirror. Read on Tradier 5-minute bars, so it
# has three bars by the first 09:45 run.
#
# Like the news veto it can only REMOVE: nothing here proposes a trade, and a
# structure still has to clear EV, edge and the constraints first. Unlike the
# news veto it fires often -- a tape that is not clearly one-sided fails one of
# four tests most of the time -- which is the point of it and also the cost.
#
# UNMEASURED at the time it went live. TRADING_DTE0_VWAP_GATE=record logs the
# same lines and refuses nothing, which is how it should have started.
VWAP_GATE = vwap_gate.MODE          # veto | record | off

# THE MACRO READ, AS AN ASYMMETRIC VETO ON DIRECTION.
#
# The case: macro turns during the session. Yields drop, crude drops, the tape
# goes risk-on at 11:00 -- and a put spread on a single name is then the worst
# structure on the board however good that name's own news looked at 09:30.
#
# NOTHING GUARDED THAT. This script consults no breadth, no VIX, no yields, no
# oil, and the engine's objective gates are one-sided by design -- nodes.py
# says so in as many words: "Rising yields hurt QQQ; falling yields are broadly
# supportive." They refuse LONG exposure into risk-off. Not one of them refuses
# a SHORT structure into risk-on. That half of the board was unguarded.
#
# WHY THE TWO SIDES GET DIFFERENT THRESHOLDS. The base rates are not
# symmetric, so a symmetric rule cannot be right. Over the 17 QQQ verdicts on
# record -- BEARISH 9, NEUTRAL 7, BULLISH 1, and VERY_* exactly zero:
#
#   refuse PUT  spreads when macro BULLISH+       fires  5.9% of sessions
#   refuse CALL spreads when macro BEARISH+       fires 52.9% of sessions
#
# The put side is rare and cheap: this read almost never calls the tape
# bullish, so the gate stays out of the way and speaks up exactly when it
# has something to say. The call side would refuse over half of all
# sessions -- and BEARISH days ran -0.085% against NEUTRAL's -0.040%, four and
# a half basis points apart, which does not buy a refusal rate like that. That
# is the August macro gate, which refused 55 of 55 cycles on a day QQQ rose
# $6.50 off its low. So the call side is held at VERY_BEARISH: dormant on this
# record, live the moment the read ever calls a real crash.
#
# GATING ON VERY_* ALONE WAS DEAD CODE. The first version of this refused only
# VERY_BULLISH and VERY_BEARISH. Neither has ever printed, so it could not fire
# on any session on record -- a switch that reads as a conservative default and
# is in fact a no-op.
#
# WHAT IS STILL UNMEASURED, PLAINLY. The evidence for the put side is one
# session: the single BULLISH day returned +0.959%, where a put spread would
# have lost. n=1 is not a result. It is armed because the COST is bounded by
# how rarely it fires, not because the edge is established -- and because the
# alternative is leaving the risk-on case with no guard at all. The hourly
# re-grade shipped today is what starts generating the rows to judge it on;
# until then every run logs the read beside the decision.
MACRO_VETO = os.getenv("TRADING_DTE0_MACRO_VETO", "true").lower() == "true"
MACRO_SYMBOL = os.getenv("TRADING_DTE0_MACRO_SYMBOL", "QQQ")
# TWO GATES, AND THE SECOND ONE IS THE POINT.
#
# LEVEL. The tail guard. Refuses a structure the tape is extremely against.
# Asymmetric because the base rates are: this read prints BEARISH on 9 of 17
# sessions and BULLISH on 1, so refusing puts into a bullish tape costs 5.9%
# of sessions while refusing calls into a bearish one costs 52.9%. A level
# gate on the call side is therefore held at VERY_BEARISH.
# SYMMETRIC AS OF 2026-09-15, BECAUSE THE INSTRUMENT CHANGED UNDER IT.
#
# This was asymmetric -- puts refused on BULLISH+, calls only on VERY_BEARISH --
# and the reason was specific: the TEXT read printed BEARISH on 53% of
# sessions, so refusing calls at plain BEARISH would have stood the book down
# on half of all days. That is the August macro gate, which refused 55 of 55
# cycles on a day QQQ rose $6.50.
#
# THAT READ NO LONGER DRIVES THIS GATE. The source is now crude/10Y/VIX
# (source='objective'), and arithmetic on three prices has no reason to lean
# bearish the way a doom-weighted keyword list did. Its first 6 readings were
# all BULLISH -- one rising afternoon, not a distribution, but the tilt the
# asymmetry was built to survive is gone with the thing that produced it.
#
# So the gate now says what a macro read should say: good macro favours call
# debits, bad macro favours put spreads.
#
#     BULLISH / VERY_BULLISH   ->  put debits refused
#     BEARISH / VERY_BEARISH   ->  call debits refused
#     NEUTRAL                  ->  both allowed
#
# NEUTRAL is a real band, not a knife edge: the objective score averages three
# clamped channels and only commits past +/-0.25, so an ordinary day leaves
# both sides open. If this turns out to refuse too much, the lever is the
# MARGIN in macro_objective.py, not a return to asymmetry -- the asymmetry was
# a workaround for a biased instrument, not a principle.
MACRO_REFUSE_BEARISH_ON = {"BULLISH", "VERY_BULLISH"}
MACRO_REFUSE_BULLISH_ON = {"BEARISH", "VERY_BEARISH"}
# THE ENGINE'S YIELD SPIKE, READ DIRECTLY (2026-09-23, section 222). The macro
# verdict above refreshes every 15 minutes; the engine's own risk-off check
# (nodes.py, 10Y >= TRADING_TNX_SPIKE_BPS above the session open) runs every
# minute. On 09-23 the flash PMI hit at 09:45 and the 10Y was +4.3bp by 10:06
# -- the engine went risk-off, this script never knew. A spike now refuses
# BULLISH (call debit) structures outright; bearish ones stay allowed, which is
# the direction a rates shock pays. 0 disables.
YIELD_SPIKE_REFUSES_CALLS = os.getenv(
    "TRADING_DTE0_YIELD_SPIKE_VETO", "true").lower() == "true"
TNX_SPIKE_BPS = float(os.getenv("TRADING_TNX_SPIKE_BPS", "4.0") or 0)

# DELTA -- THE TAPE TURNING, WHICH IS THE CASE A LEVEL GATE CANNOT SEE.
#
# The scenario, put twice and correctly: at 11:00 yields and crude drop and
# the tape goes risk-on, so a put spread is the wrong structure. At 14:00
# crude spikes on Iran, yields spike on the Fed, the tape turns bearish, and a
# call debit spread is the wrong structure. Both are TURNS. The word in both
# cases was "changes" and "turns", not "is".
#
# A LEVEL GATE IS THE WRONG INSTRUMENT FOR A TURN. BEARISH is the modal
# reading of this feed -- 53% of sessions -- and novelty_check.py found the
# tilt suspect: BEARISH on 10 of 14 sessions, 5 of 10 on direction, "unmoved
# while the tape reversed". Gating a level that is the background state refuses
# half the book for noise. Gating the CHANGE fires only when the read actually
# moves, which is rare by construction and is the event being described.
#
# THIS WAS NOT OBSERVABLE UNTIL TODAY. The verdict was written once at 09:30,
# so there was exactly one reading per session and no delta existed to gate
# on. The hourly re-grade is the precondition for this gate, not a separate
# feature -- which is also why it cannot be backtested: every historical day
# has a single verdict, so this would have fired zero times on the record.
# It is armed on its shape, not on a measurement, and that is stated plainly
# rather than dressed up. dte0_shadow is empty (0 rows), so there is no paper
# record to check it against either; that is worth fixing separately.
#
# Symmetric, unlike the level gate, because a turn is a turn whichever way it
# goes -- and because a deterioration from NEUTRAL to BEARISH is information
# in a way that a standing BEARISH is not.
MACRO_DELTA_GATE = os.getenv("TRADING_DTE0_MACRO_DELTA", "true").lower() == "true"
# How far the read must move from the day's OPENING verdict to count as a
# turn. 1 step: NEUTRAL -> BEARISH, or BEARISH -> VERY_BEARISH.
MACRO_DELTA_STEPS = float(os.getenv("TRADING_DTE0_MACRO_DELTA_STEPS", "1"))
MACRO_ORD = {"VERY_BEARISH": -2.0, "BEARISH": -1.0, "NEUTRAL": 0.0,
             "BULLISH": 1.0, "VERY_BULLISH": 2.0}


def _macro_verdict() -> "tuple | None":
    """(verdict, confidence, asof, opening_verdict) for the macro name, or None.

    READS THE SOURCE TABLE, NOT news_verdicts, and the difference is 45 minutes.
    news_verdicts is written by news_watch once an hour at :25, while
    macro_objective writes crude/10Y/VIX every fifteen. Going through the
    hourly table would have capped this gate's reaction time at an hour --
    a turn at 11:35 invisible until 12:25 -- which is most of the reason the
    price read was chosen over the text one. classify_day() takes the latest
    row at or before now, so the gate sees the 15-minute cadence it was armed
    for.

    Falls back to news_verdicts if the direct read yields nothing, so a symbol
    still graded only by the hourly watcher keeps working.
    """
    try:
        day = datetime.now(NY).date()
        g = classify_day(MACRO_SYMBOL.upper(), day)
        with psycopg2.connect(_dsn()) as c, c.cursor() as cur:
            cur_row = None
            if g and g.get("graded") and g.get("headline_count"):
                cur_row = (g["verdict"], g["confidence"], datetime.now(NY))
            else:
                cur.execute("SELECT verdict, confidence, asof FROM news_verdicts "
                            "WHERE symbol=%s AND trading_day=%s",
                            (MACRO_SYMBOL.upper(), day))
                cur_row = cur.fetchone()
            if not cur_row:
                return None
            # The OPENING read, for the delta. verdict_at() takes the history
            # row in force at the cutoff, which is the same function the
            # measurement scripts use -- one definition of "at the open", not
            # two that can drift apart.
            open_row = verdict_at(cur, MACRO_SYMBOL.upper(), day)
            return cur_row + (open_row[0] if open_row else None,)
    except Exception:
        logger.warning("Macro verdict unreadable — single-name news is "
                       "unaffected.", exc_info=True)
        return None


def _passes(r: dict) -> "str | None":
    """None if the row clears all three constraints, else why it did not."""
    cost, w, spot = float(r["cost"]), float(r["w"]), float(r["spot"])
    if w <= 0 or cost <= 0:
        return "degenerate price"
    ew = cost / w
    if not (MIN_EW <= ew <= MAX_EW):
        return f"entry {ew:.0%} of width, outside {MIN_EW:.0%}-{MAX_EW:.0%}"
    if BOOK == "weekly" and float(r.get("pwin") or 0) < WEEKLY_MIN_PWIN:
        return f"Pwin {float(r.get('pwin') or 0):.0%} below the weekly floor {WEEKLY_MIN_PWIN:.0%}"
    # For a debit the long leg is the strike nearer the money: the LOW strike
    # on a call, the HIGH strike on a put.
    bullish = r.get("direction") != "bearish"
    long_k = float(r["lo"]) if bullish else float(r["hi"])
    intr = (min(max(spot - long_k, 0.0), w) if bullish
            else min(max(long_k - spot, 0.0), w))
    extr = cost - intr
    if extr < 0:
        return "negative extrinsic — crossed or stale quote"
    ex_pct = extr / cost * 100.0
    if ex_pct > MAX_EXTRINSIC:
        return f"extrinsic {ex_pct:.0f}% of premium, above {MAX_EXTRINSIC:.0f}%"
    tgt_mark = cost * (1 + TARGET_PCT / 100.0)
    if tgt_mark > w:
        return f"+{TARGET_PCT:.0f}% target above the {w:.1f} width"
    tgt_spot = (long_k + tgt_mark) if bullish else (long_k - tgt_mark)
    atr = float(r.get("atr") or 0)
    if atr <= 0:
        return "no ATR"
    move_atr = abs(tgt_spot - spot) / atr
    if move_atr > MAX_TARGET_ATR:
        return f"target needs {move_atr:.2f} ATR, above {MAX_TARGET_ATR:.2f}"
    # What the short leg PAYS. See MIN_SHORT_PAYS_PCT.
    la, sb = r.get("long_ask"), r.get("short_bid")
    if MIN_SHORT_PAYS_PCT > 0 and la is not None and sb is not None and float(la) > 0:
        pays = float(sb) / float(la) * 100.0
        if pays < MIN_SHORT_PAYS_PCT:
            return (f"short leg bid {float(sb):.2f} is {pays:.1f}% of the {float(la):.2f} long, "
                    f"under {MIN_SHORT_PAYS_PCT:.0f}% — a long call, not a spread")
    # The short leg has to be somewhere price can plausibly reach, or selling
    # it earns nothing and only caps the upside.
    short_k = float(r["hi"]) if long_k == float(r["lo"]) else float(r["lo"])
    short_atr = abs(short_k - spot) / atr
    if short_atr > MAX_SHORT_ATR:
        return (f"short strike {short_atr:.2f} ATR out, above {MAX_SHORT_ATR:.2f} "
                f"— it would fetch almost nothing and cap the upside for it")
    r["_short_atr"] = short_atr
    r["_ew"], r["_ex_pct"], r["_move_atr"] = ew, ex_pct, move_atr
    r["_target_spot"], r["_long"] = tgt_spot, long_k
    return None


def _optflow_expiries(exp: str, symbol: str) -> list:
    """The traded expiry plus the next listed one, for the options-flow read."""
    out = [exp]
    try:
        exps = sorted(str(e) for e in (tradier_orders.expirations(symbol) or []))
        later = [e for e in exps if e > exp]
        if later:
            out.append(later[0])
    except Exception:
        pass
    return out


def _quote_pct(symbol: str, expiry: str) -> "float | None":
    """Median near-ATM bid-ask as a share of mid. The cost of participating."""
    try:
        spot = float(fetch_spot(symbol) or 0)
        chain = fetch_option_chain(expiry, symbol)
    except Exception:
        return None
    if not spot or not chain:
        return None
    pcts = []
    for (kind, strike), q in chain.items():
        if kind != "call" or abs(strike - spot) > spot * 0.03:
            continue
        if q.bid <= 0 or q.ask <= 0:
            continue
        mid = (q.bid + q.ask) / 2
        if mid > 0:
            pcts.append((q.ask - q.bid) / mid * 100.0)
    pcts.sort()
    return pcts[len(pcts) // 2] if pcts else None


def _dsn() -> str:
    return (os.getenv("DATABASE_URL", "")
            .replace("postgresql+psycopg2://", "postgresql://")
            .replace("postgresql+asyncpg://", "postgresql://"))


def _exits_today(symbols: set, now: datetime) -> dict:
    """{symbol: (last_close, last_pnl, closes_today)} from trading_history.

    Orphan exits book here too, so a position closed by the stall, the target
    or the flatten all count -- which is what makes "re-enter after it exits"
    mean the same thing however it left.
    """
    out: dict = {}
    if not symbols:
        return out
    try:
        with psycopg2.connect(_dsn()) as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT underlying, max(closed_at), count(*),
                       (array_agg(realized_pnl_dollars ORDER BY closed_at DESC))[1]
                FROM trading_history
                WHERE closed_at IS NOT NULL
                  AND closed_at >= %s AND underlying = ANY(%s)
                GROUP BY underlying
            """, (now.date(), list(symbols)))
            for sym, last, n, pnl in cur.fetchall():
                out[sym] = (last, float(pnl or 0.0), int(n))
    except Exception:
        # FAIL CLOSED: not knowing what already traded means not rotating.
        logger.warning("Could not read exit history — rotation stands down.",
                       exc_info=True)
        return {s: (now, 0.0, MAX_ROTATIONS) for s in symbols}
    return out


def _weekly_type_for(exp_iso: str, today: "date | None" = None) -> str:
    """'w7' if the expiry is WEEKLY_LONG_MIN_DAYS or more calendar days out, else 'w3'."""
    days = (date.fromisoformat(exp_iso) - (today or datetime.now(NY).date())).days
    long_min = int(os.getenv("TRADING_WEEKLY_LONG_MIN_DAYS", "5") or 5)
    return "w7" if days >= long_min else "w3"


def _weekly_budget(wtype: str) -> float:
    """TRADING_W3_MAX_BUDGET / TRADING_W7_MAX_BUDGET, else the old shared weekly budget."""
    raw = os.getenv(f"TRADING_{wtype.upper()}_MAX_BUDGET", "").strip()
    try:
        return float(raw) if raw else WEEKLY_MAX_BUDGET
    except ValueError:
        return WEEKLY_MAX_BUDGET


def _bucket_exposure(book: str, today: str, wtype: "str | None" = None) -> "float | None":
    """Dollars already at risk in this book's open spreads; None if unreadable.

    Section 238. The budget used to be a per-RUN allowance, so every 15-minute
    run started from the full amount whatever earlier runs had opened. It is
    now a cap on the whole bucket. Membership is by expiry, excluding QQQ (the
    engine's own bucket): same-day spreads for the 0DTE book, later expiries
    for the weekly book. Manual spreads on those names count too -- it is the
    money that is in use, whoever placed it.
    """
    try:
        from trading_engine import orphans
        total = 0.0
        for st in orphans.open_structures():
            if st["root"] == "QQQ":
                continue
            same_day = st["expiry"] == today
            if (book == "weekly") == same_day:
                continue
            if wtype and orphans.weekly_type(st) != wtype:
                continue            # section 245: the other weekly bucket's position
            width = abs(st["short_strike"] - st["long_strike"])
            e = abs(float(st["entry"]))
            per = (width - e) if st["credit"] else e
            total += max(per, 0.0) * 100.0 * int(st["qty"])
        return total
    except Exception:
        logger.warning("Could not read open positions for the bucket cap.", exc_info=True)
        return None


def _rotation_filter(syms: list, now: datetime) -> list:
    """Which names may take a NEW position right now."""
    cutoff = WEEKLY_ROTATE_CUTOFF if BOOK == "weekly" else ROTATE_CUTOFF
    hh, mm = (int(x) for x in cutoff.split(":"))
    if now.time() >= dtime(hh, mm):
        logger.info("past the %s rotation cutoff — no new entries.", cutoff)
        return []
    exits = _exits_today(set(syms), now)
    keep = []
    for sym in syms:
        rec = exits.get(sym)
        if not rec:
            keep.append(sym)
            continue
        last, pnl, n = rec
        if n >= MAX_ROTATIONS:
            logger.info("%-5s %d exits today, at the %d cap — done for the day.",
                        sym, n, MAX_ROTATIONS)
            continue
        mins = (now - last).total_seconds() / 60.0 if last else 999.0
        if mins < ROTATE_COOLDOWN_MIN:
            logger.info("%-5s exited %.0f min ago (%+.0f) — cooling down for "
                        "another %.0f min.", sym, mins, pnl,
                        ROTATE_COOLDOWN_MIN - mins)
            continue
        logger.info("%-5s exited %.0f min ago (%+.0f), %d today — eligible again.",
                    sym, mins, pnl, n)
        keep.append(sym)
    return keep


def _held_today(symbols: set, expiry: str) -> set:
    """Underlyings the account already holds an option in for `expiry`.

    A rerun must not double a position, and the broker is the only honest
    source for what is held -- the same reasoning as the orphan close guard.
    """
    held = set()
    try:
        ymd = expiry.replace("-", "")[2:]
        for p in (tradier_orders.open_positions() or []):
            sym = str(p.get("symbol", ""))
            for s in symbols:
                if sym.startswith(s) and ymd in sym:
                    held.add(s)
    except Exception:
        logger.warning("Could not read positions — refusing to place anything.",
                       exc_info=True)
        return symbols          # fail CLOSED: unknown means do not trade
    return held


def _resolve_expiry(arg: str, book: str, today: "date | None" = None) -> str:
    """An ISO date from --expiry.

    ''            today for the 0DTE book; 'friday' for the weekly book
    'friday'     the next Friday at least WEEKLY_MIN_DAYS calendar days out --
                 this Friday from Monday to Wednesday, next Friday after
    '+N'         the first Friday at least N days out
    YYYY-MM-DD   as given
    """
    today = today or date.today()
    if not arg:
        arg = "friday" if book == "weekly" else today.isoformat()
    if arg.startswith("+") or arg == "friday":
        min_days = int(arg[1:]) if arg.startswith("+") else WEEKLY_MIN_DAYS
        d = today
        while d.weekday() != 4 or (d - today).days < min_days:
            d = d.fromordinal(d.toordinal() + 1)
        return d.isoformat()
    return arg


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default=SYMBOLS)
    ap.add_argument("--budget", type=float, default=1500.0)
    ap.add_argument("--max-trades", type=int, default=3)
    ap.add_argument("--by", default="ev", choices=("ev", "evpct", "prob", "edge"))
    ap.add_argument("--expiry", default="",
                    help="YYYY-MM-DD, \"friday\", or \"+N\" (first Friday >= N days out); blank = today, or friday for --book weekly")
    ap.add_argument("--book", default="dte0", choices=("dte0", "weekly"))
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--rotate", action="store_true",
                    help="re-entry pass: skip names already held, still in "
                         "cooldown, or at the daily cap. Requires "
                         "TRADING_DTE0_ROTATE=true as well.")
    args = ap.parse_args()

    # THE BUCKET BUDGET IS THE BUDGET (2026-09-24, section 227): set per bucket
    # on /desk/settings. --budget (the cron passes 5000) is ignored when it
    # differs, and said so, so raising the setting in the UI actually raises it.
    syms = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    exp = _resolve_expiry(args.expiry, args.book)
    # SECTION 245: the weekly book is two buckets, 3-day and 7-day, each with
    # its own switch and budget. A run is the type of the expiry it buys --
    # WEEKLY_LONG_MIN_DAYS (5) or more calendar days out is 7-day -- the same
    # rule orphans.weekly_type() applies to the positions it then manages.
    wtype = _weekly_type_for(exp) if args.book == "weekly" else None
    budget = (_weekly_budget(wtype) if wtype else MAX_BUDGET)
    if args.budget != budget:
        logger.info("Budget $%.0f from the %s bucket setting (--budget %.0f ignored).",
                    budget, wtype or args.book, args.budget)
    if args.book == "weekly":
        global MIN_EW, MAX_EW, MAX_SHORT_ATR, MAX_EXTRINSIC, MAX_TARGET_ATR, BOOK
        BOOK = "weekly"
        MIN_EW, MAX_EW, MAX_SHORT_ATR = WEEKLY_MIN_EW, WEEKLY_MAX_EW, WEEKLY_MAX_SHORT_ATR
        MAX_EXTRINSIC, MAX_TARGET_ATR = WEEKLY_MAX_EXTRINSIC, WEEKLY_MAX_TARGET_ATR
        if args.by == "ev":
            args.by = "edge"        # the board's default; ev alone finds the OTM lottery ticket
        logger.info("WEEKLY BOOK: expiry %s (%d days), budget $%.0f over %d slot(s); exits are "
                    "the LATER ladder (-45%% stop, 30-min stall from +25%%, 0.25 ATR "
                    "give-back, 95%% intrinsic target, +70%% mark target, 09:45 hold).",
                    exp, (date.fromisoformat(exp) - date.today()).days, budget, args.max_trades)
    now = datetime.now(NY)

    # A SCHEDULED MACRO EVENT. This gate lived only in trading_engine/nodes.py,
    # and this script is a SECOND live entry path -- it runs on its own cron
    # every fifteen minutes and never asked the calendar. Arming
    # TRADING_EVENT_BLACKOUT therefore stood down the engine and left this one
    # trading, which is the worst of both: the protection looks armed and half
    # the book ignores it. Found on 2026-09-16, an FOMC day, nine minutes
    # before the open.
    #
    # Placed before the chain fetch so a stood-down day costs no API calls and
    # says plainly why it did nothing.
    event_note = describe_event(now)
    if event_note:
        logger.info("%s", event_note)
    if event_blackout_active(now):
        logger.info("Scheduled macro event — no new 0DTE entries today.")
        return

    # DOES A CONTRACT EVEN EXIST TODAY? Ask before doing anything else.
    #
    # QQQ has daily expiries; single names do not. On 2026-09-15, a Tuesday,
    # QQQ listed 09-15/16/17/18 while NVDA, TSLA, AAPL, MU and AVGO all started
    # at 09-16 -- so there was no 0DTE contract on any of the nine names.
    #
    # The run still did the full sweep and reported "No chain was tight enough
    # to trade today", which reads as a LIQUIDITY judgement about a market that
    # was examined. Nothing was examined; the contracts do not exist. A wrong
    # explanation for a quiet day is worse than no explanation, because it gets
    # believed -- someone reading that line would go looking at spread widths.
    #
    # This does not change what trades. It changes what the log claims, and it
    # skips a pointless sweep of empty chains.
    if not args.expiry:
        try:
            have = [x for x in syms
                    if (not tradier_orders.expirations(x))       # cannot tell
                    or exp in tradier_orders.expirations(x)]
            if not have:
                logger.info("No %s expiry exists for any of %s. Single names "
                            "list Mon/Wed/Fri-style expiries, not daily; QQQ "
                            "is the one that trades every session. Nothing to "
                            "do today — this is the calendar, not the market.",
                            exp, ",".join(syms))
                return
            if len(have) < len(syms):
                logger.info("%d of %d names have a %s expiry: %s",
                            len(have), len(syms), exp, ",".join(have))
                syms = have
        except Exception:
            logger.warning("Could not check expiries — continuing with all "
                           "names.", exc_info=True)

    if args.rotate:
        if not ROTATE_ENABLED:
            logger.warning("--rotate given but TRADING_DTE0_ROTATE is not true "
                           "— nothing done.")
            return
        syms = _rotation_filter(syms, now)
        if not syms:
            logger.info("no name is eligible for a new position right now.")
            return
    live = args.live and LIVE_ENABLED
    if args.live and not LIVE_ENABLED:
        logger.warning("--live given but TRADING_DTE0_LIVE is not true — DRY RUN.")
    # THE BUCKET SWITCHES (2026-09-24, section 227): single-stock 0DTE and
    # single-stock weekly can be turned off separately. Off = this run places
    # nothing (it still screens and logs, as a dry run); open positions are
    # managed by orphans.py either way.
    bucket_key = (f"TRADING_BUCKET_STOCK_{wtype.upper()}" if wtype
                  else "TRADING_BUCKET_STOCK_0DTE")
    if os.getenv(bucket_key, "false").lower() != "true":
        if live:
            logger.warning("%s is OFF — DRY RUN, no orders this run.", bucket_key)
        live = False

    # The per-trade allowance has to be known BEFORE selection, or a symbol
    # whose best-EV structure is unaffordable gets dropped entirely instead of
    # falling back to one that fits. MU on 2026-09-12 was exactly that: its
    # top row was a 50-wide at $1,936 and the name vanished from the plan.
    per_trade_cap = budget / max(args.max_trades, 1)
    # SECTION 238: THE BUDGET IS A CAP ON THE BUCKET, AND NOTHING IS SENT
    # WITHOUT THE MONEY FOR IT. Available = budget minus what this book already
    # has open, and never more than the account's option buying power.
    exposure = _bucket_exposure(args.book, now.strftime("%y%m%d"), wtype)
    bp = tradier_orders.buying_power() if live else None
    if exposure is None:
        if live:
            logger.warning("bucket exposure unreadable — DRY RUN, no orders this run.")
        live = False
        exposure = 0.0
    available = max(budget - exposure, 0.0)
    if live:
        if bp is None:
            logger.warning("buying power unreadable — DRY RUN, no orders this run.")
            live = False
        else:
            available = min(available, bp)
    logger.info("bucket %s: budget $%.0f, already open $%.0f, buying power %s -> "
                "$%.0f available this run", args.book, budget, exposure,
                "n/a (dry run)" if bp is None else f"${bp:.0f}", available)
    per_trade_cap = min(per_trade_cap, available)

    # WHY NOTHING CLEARED IS AS IMPORTANT AS WHAT DID. Five filters run in
    # series and a silent "nothing cleared" leaves you unable to tell a quiet
    # market from a knob set wrong. Rejections are tallied by reason.
    from collections import Counter
    rejects: Counter = Counter()

    # Liquidity first: a chain too wide to exit is not worth ranking.
    tradeable = []
    for sym in syms:
        qp = _quote_pct(sym, exp)
        if qp is None:
            logger.info("%-5s no quote read — skipped.", sym)
            continue
        if qp > MAX_QUOTE_PCT:
            logger.info("%-5s quote %.1f%% of mid, above the %.1f%% ceiling — "
                        "skipped. It cannot pay for its own exit.",
                        sym, qp, MAX_QUOTE_PCT)
            continue
        logger.info("%-5s quote %.1f%% of mid — tradeable.", sym, qp)
        tradeable.append(sym)
    if not tradeable:
        logger.info("No chain was tight enough to trade today.")
        return
    syms = tradeable

    # THE MACRO READ, LOGGED WHETHER OR NOT IT GATES. This line is the row
    # macro_outcome.py needs to eventually answer whether a bad tape actually
    # precedes a bad session for single names; without it the question stays
    # open forever and the gate above stays dark on no evidence rather than on
    # evidence.
    # THE RAW FACTORS, NOT JUST THE VERDICT LABEL.
    #
    # "macro BEARISH" tells you nothing about WHY, and the why is the part that
    # changes hourly. Crude, the 10Y and VIX are what the macro read is now
    # built from (source='objective'), so the line prints them beside it: a
    # refusal that says "crude -2.3%, 10Y -1.8bp, VIX -3.2%" can be argued with,
    # and one that says "BEARISH" cannot.
    factors = ""
    try:
        from trading_engine.data_feed import fetch_oil, fetch_tnx, fetch_vix

        bits = []
        for name, fn, unit in (("crude", fetch_oil, "%"), ("10Y", fetch_tnx, "bp"),
                               ("VIX", fetch_vix, "%")):
            try:
                rd = fn()
                if rd is None:
                    continue
                mvv = (rd.change_bps if unit == "bp" else rd.change_pct)
                bits.append(f"{name} {mvv:+.2f}{unit}")
            except Exception:
                continue
        factors = " | ".join(bits)
    except Exception:
        pass

    macro = _macro_verdict()
    mv = mopen = None
    mdelta = 0.0
    if macro:
        mv, mc, masof, mopen = macro[0], macro[1] or 0.0, macro[2], macro[3]
        mdelta = MACRO_ORD.get(mv, 0.0) - MACRO_ORD.get(mopen or mv, 0.0)
        turn = ("no turn" if abs(mdelta) < MACRO_DELTA_STEPS
                else f"TURNED {'bullish' if mdelta > 0 else 'bearish'} "
                     f"({mopen} -> {mv})")
        logger.info("macro read (%s): %s %.2f%s | open %s | %s | level gate %s,"
                    " delta gate %s", MACRO_SYMBOL, mv, mc,
                    f" as of {masof:%H:%M}" if masof else "", mopen or "-", turn,
                    "on" if MACRO_VETO else "off",
                    "on" if MACRO_DELTA_GATE else "off")
        if factors:
            logger.info("   driven by: %s", factors)
        logger.info("   -> %s spreads %s, %s spreads %s",
                    "PUT", "REFUSED" if mv in MACRO_REFUSE_BEARISH_ON else "allowed",
                    "CALL", "REFUSED" if mv in MACRO_REFUSE_BULLISH_ON else "allowed")
    else:
        logger.info("macro read (%s): none today — both macro gates stand "
                    "down.%s", MACRO_SYMBOL,
                    f"  (factors: {factors})" if factors else "")

    try:
        from trading_engine.macro_calendar import releases_on
        for rel in releases_on():
            logger.info("data release today: %s ET %s%s", rel.get("time"), rel.get("name"),
                        f" — {rel['note']}" if rel.get("note") else "")
    except Exception:
        pass

    # The engine's minute-by-minute yield spike, independent of the verdict.
    yield_spike = None
    if YIELD_SPIKE_REFUSES_CALLS and TNX_SPIKE_BPS > 0:
        try:
            from trading_engine.data_feed import fetch_tnx
            tnx = fetch_tnx()
            if tnx.change_bps >= TNX_SPIKE_BPS:
                yield_spike = (f"10Y {tnx.level:.3f}% is {tnx.change_bps:+.1f}bp from the "
                               f"{tnx.session_open:.3f}% open")
                logger.info("YIELD SPIKE: %s (>= %.1fbp) — CALL debits refused, PUT debits "
                            "allowed.", yield_spike, TNX_SPIKE_BPS)
        except Exception:
            logger.warning("10Y read failed — the yield-spike veto stands down this run.",
                           exc_info=True)

    # SECTION 241: the engine's own macro verdict (GOOD/BAD, written to
    # trading_logs every minute). BAD means unsafe to be long -- the engine
    # already takes only bearish entries then; with TRADING_MACRO_BAD_PUTS_ONLY
    # the rotation does the same. A verdict older than 10 minutes is ignored.
    from trading_engine import structure_gates
    engine_macro = None
    if structure_gates.macro_bad_puts_only():
        try:
            with psycopg2.connect(_dsn()) as c, c.cursor() as cur:
                cur.execute("SELECT market_sentiment FROM trading_logs WHERE timestamp > now() - "
                            "interval '10 minutes' AND market_sentiment <> '' "
                            "ORDER BY timestamp DESC LIMIT 1")
                row = cur.fetchone()
                engine_macro = row[0] if row else None
        except Exception:
            logger.warning("Engine macro verdict unreadable.", exc_info=True)
        logger.info("engine macro verdict: %s — %s", engine_macro or "none in the last 10 min",
                    "PUT spreads only" if engine_macro == "BAD" else "both sides allowed")
    week_ctx: dict = {}

    def _ctx(sym: str):
        if sym not in week_ctx:
            week_ctx[sym] = structure_gates.week_context(sym)
            c = week_ctx[sym]
            if c:
                logger.info("%-5s week %.2f-%.2f spot %.2f = %s of range | hourly %s 20-SMA | 5-min %s 20-SMA",
                            sym, c["week_low"], c["week_high"], c["spot"],
                            "?" if c["weekpos"] is None else f"{c['weekpos']:.0%}",
                            {True: "below", False: "above", None: "?"}[c["below_1h"]],
                            {True: "below", False: "above", None: "?"}[c["below_5m"]])
        return week_ctx[sym]

    # Best surviving candidate per symbol per side.
    best: dict = {}
    flow_logged: set = set()
    for side in ("call", "put"):
        try:
            rr_lo, rr_hi = WEEKLY_RR if args.book == "weekly" else (0.0, 0.0)
            res = rank(syms, side, by=args.by, top=60, structure="debit", expiry=exp,
                       rr_min=rr_lo, rr_max=rr_hi)
        except Exception:
            logger.warning("rank() failed for %s — skipped.", side, exc_info=True)
            continue
        for r in res.get("rows", []):
            why = _passes(r)
            if why:
                rejects[why.split(",")[0].split(" -- ")[0]] += 1
                continue
            # A NEGATIVE EDGE IS NOT A TRADE. Ranking by EV alone happily
            # returned META at Pwin 50.1% against a 54.3% break-even -- the
            # best of a bad set is still bad, and "best available" is not a
            # reason to buy something the screener prices as losing.
            if r["ev_dem"] <= 0 or r["pwin"] <= r["need"]:
                rejects["negative edge or EV"] += 1
                continue
            # Affordability is a selection criterion, not a post-check.
            if float(r["cost"]) * 100 > per_trade_cap:
                rejects["above the per-trade budget"] += 1
                continue
            # GATE ORDER: MACRO FIRST, THEN THE NAME'S OWN NEWS.
            #
            # Both are hard refusals, so the order cannot change WHICH trades
            # survive -- but it decides which reason gets logged and counted,
            # and that is what anyone reads afterwards to understand a quiet
            # day. Macro is the wider claim: if the tape is against the
            # structure, that is the more useful thing to have been told than
            # a single name's headline sentiment.
            #
            # The macro tape, twice: the LEVEL as a tail guard (asymmetric --
            # see the base rates beside MACRO_VETO), and the DELTA as the
            # intraday-turn guard (symmetric -- a turn is a turn either way).
            bullish = r.get("direction") != "bearish"
            if bullish and yield_spike:
                logger.info("%s %s %.0f/%.0f refused: the structure is bullish and yields "
                            "are spiking (%s).", r["sym"], side.upper(),
                            float(r["lo"]), float(r["hi"]), yield_spike)
                rejects["bullish into a yield spike"] += 1
                continue
            if MACRO_VETO and mv:
                if mv in (MACRO_REFUSE_BULLISH_ON if bullish
                          else MACRO_REFUSE_BEARISH_ON):
                    logger.info("%s %s %.0f/%.0f refused: the structure is %s "
                                "and the %s tape is %s.", r["sym"], side.upper(),
                                float(r["lo"]), float(r["hi"]),
                                "bullish" if bullish else "bearish",
                                MACRO_SYMBOL, mv)
                    rejects["against the macro tape"] += 1
                    continue
            if bullish and engine_macro == "BAD":
                logger.info("%s %s %.0f/%.0f refused: bullish and the engine's macro is BAD "
                            "(puts only).", r["sym"], side.upper(), float(r["lo"]), float(r["hi"]))
                rejects["bullish with engine macro BAD"] += 1
                continue
            if structure_gates.weekrange_on():
                _why = structure_gates.weekrange_refusal(bullish, _ctx(r["sym"]))
                if _why:
                    logger.info("%s %s %.0f/%.0f refused: %s.", r["sym"], side.upper(),
                                float(r["lo"]), float(r["hi"]), _why)
                    rejects["week-range guard"] += 1
                    continue
            _tf = "hourly" if args.book == "weekly" else "5min"
            if structure_gates.pullback_on(_tf):
                _why = structure_gates.pullback_refusal(bullish, _ctx(r["sym"]), _tf)
                if _why:
                    logger.info("%s %s %.0f/%.0f refused: %s.", r["sym"], side.upper(),
                                float(r["lo"]), float(r["hi"]), _why)
                    rejects["pullback trigger not met"] += 1
                    continue
            if MACRO_DELTA_GATE and abs(mdelta) >= MACRO_DELTA_STEPS:
                # A bullish structure dies on a bearish turn and vice versa.
                if (bullish and mdelta <= -MACRO_DELTA_STEPS) or                    ((not bullish) and mdelta >= MACRO_DELTA_STEPS):
                    logger.info("%s %s %.0f/%.0f refused: the structure is %s "
                                "and %s TURNED %s since the open (%s -> %s).",
                                r["sym"], side.upper(), float(r["lo"]),
                                float(r["hi"]), "bullish" if bullish else "bearish",
                                MACRO_SYMBOL,
                                "bullish" if mdelta > 0 else "bearish",
                                mopen, mv)
                    rejects["the macro tape turned against it"] += 1
                    continue
            # The news read, as a veto on direction -- ABOVE A CONFIDENCE
            # FLOOR, which this did not have until 2026-09-14.
            #
            # The engine's equivalent gate (nodes.py, NEWS_DIRECTION) has
            # required 0.70 since it shipped. This one checked the verdict and
            # ignored the confidence entirely, so the same news read gated two
            # books by different rules -- and the looser one is the one trading
            # nine names.
            #
            # WHAT THAT COST ON 2026-09-14:
            #
            #     MU    BEARISH 0.25   ->  all MU call debits refused, MU +2.50%
            #     TSLA  BEARISH 0.50   ->  3 call refusals logged, TSLA flat
            #     QQQ   BEARISH 0.67
            #
            # Six MU put spreads stopped out on a name that rose $22, because
            # the only side the veto left open was the wrong one. NONE of those
            # three verdicts clears 0.70, so under the engine's own rule not one
            # would have fired.
            #
            # This does NOT claim the floor makes money -- the news read has
            # still never been scored against an outcome. It claims the two
            # gates should not disagree, which is true whichever threshold is
            # right. Shared constant so they cannot drift apart again.
            if NEWS_VETO:
                verdict = r.get("news")
                conf = r.get("news_conf")
                bullish = r.get("direction") != "bearish"
                contradicts = verdict and ((bullish and verdict in NEWS_BEARISH)
                                           or ((not bullish)
                                               and verdict in NEWS_BULLISH))
                if contradicts and (conf or 0.0) < NEWS_MIN_CONF:
                    logger.info("%s %s %.0f/%.0f: the read is %s but only at "
                                "%.2f confidence, below the %.2f floor — "
                                "ignored, not refused.", r["sym"], side.upper(),
                                float(r["lo"]), float(r["hi"]), verdict,
                                conf or 0.0, NEWS_MIN_CONF)
                elif contradicts:
                    logger.info("%s %s %.0f/%.0f refused: the structure is %s "
                                "and the news read is %s (%.2f).",
                                r["sym"], side.upper(), float(r["lo"]),
                                float(r["hi"]),
                                "bullish" if bullish else "bearish", verdict,
                                conf or 0.0)
                    rejects["against the news read"] += 1
                    continue
            gate_mode = vwap_gate.effective_mode()
            if gate_mode in ("veto", "record"):
                bullish = r.get("direction") != "bearish"
                flow = vwap_gate.session_flow(r["sym"])
                ok, why = vwap_gate.gate("bullish" if bullish else "bearish", flow)
                if (r["sym"], side) not in flow_logged:
                    flow_logged.add((r["sym"], side))
                    logger.info("%s %s %s -> %s: %s", r["sym"], side.upper(),
                                vwap_gate.describe(flow),
                                "ok" if ok else ("REFUSED" if gate_mode == "veto"
                                                  else "would refuse (record after %s)"
                                                  % vwap_gate.UNTIL), why)
                r["_flow"] = vwap_gate.describe(flow)
                if not ok and gate_mode == "veto":
                    rejects["against the tape (VWAP flow)"] += 1
                    continue
            if BOOK == "weekly" and weekly_vwap_gate.MODE in ("veto", "record"):
                # THE WEEK'S VWAP, for the book that holds a week -- section
                # 210. The session gate above is a same-day read and section
                # 209 measured it carrying nothing over four sessions; this
                # one is anchored to Monday's open. Direction-keyed like the
                # rest: a call debit wants spot above the week's level with
                # the level rising, a put the mirror.
                bullish = r.get("direction") != "bearish"
                wflow = weekly_vwap_gate.read(r["sym"], float(r.get("atr") or 0))
                wok, wwhy = weekly_vwap_gate.gate("bullish" if bullish else "bearish", wflow)
                if (r["sym"], side, "week") not in flow_logged:
                    flow_logged.add((r["sym"], side, "week"))
                    logger.info("%s %s %s -> %s: %s", r["sym"], side.upper(),
                                weekly_vwap_gate.describe(wflow),
                                "ok" if wok else ("REFUSED" if weekly_vwap_gate.MODE == "veto"
                                                   else "would refuse"), wwhy)
                if not wok and weekly_vwap_gate.MODE == "veto":
                    rejects["against the week's VWAP"] += 1
                    continue
            if options_flow.MODE in ("veto", "record"):
                # THE CHAIN'S OWN VOLUME AGAINST OPEN INTEREST -- section 197.
                # Read on the traded expiry plus the next weekly, once per name
                # per run. Record by default: logged beside FLOW, refuses nothing
                # until it has been scored.
                bullish = r.get("direction") != "bearish"
                oflow = options_flow.read(r["sym"], _optflow_expiries(exp, r["sym"]),
                                          r.get("spot"))
                ook, owhy = options_flow.gate("bullish" if bullish else "bearish", oflow)
                if (r["sym"], side, "opt") not in flow_logged:
                    flow_logged.add((r["sym"], side, "opt"))
                    logger.info("%s %s %s -> %s: %s", r["sym"], side.upper(),
                                options_flow.describe(oflow),
                                "ok" if ook else ("REFUSED" if options_flow.MODE == "veto"
                                                   else "would refuse"), owhy)
                if not ook and options_flow.MODE == "veto":
                    rejects["against the options flow"] += 1
                    continue
            if r.get("conflict"):
                logger.info("%s %s refused: %s", r["sym"], side.upper(), r["conflict"])
                continue
            key = r["sym"]
            if key not in best or r["ev_dem"] > best[key]["ev_dem"]:
                r["_side"] = side
                best[key] = r

    if not best:
        logger.info("Nothing cleared the constraints on %s. That is an answer, "
                    "but here is what it was:", exp)
        for reason, n in rejects.most_common(8):
            logger.info("   %4d  %s", n, reason)
        if not rejects:
            logger.info("   the screener returned no rows at all — check the "
                        "expiry and that the chain is quoting")
        return

    chosen = sorted(best.values(), key=lambda r: -r["ev_dem"])[:args.max_trades]
    # SIZE AGAINST THE SLOT, NOT AGAINST WHAT QUALIFIED. Dividing the budget
    # by the number of survivors concentrates the whole allowance into one
    # name on a day when only one clears the filters -- which is precisely the
    # day to be smaller, not larger, since the filters just told you the rest
    # of the board was untradeable. Unspent budget stays unspent.
    per = per_trade_cap
    held = _held_today({r["sym"] for r in chosen}, exp)
    if args.book == "weekly":
        # Any expiry: a weekly on a name already held 0DTE is the same bet twice.
        try:
            held |= {r["sym"] for r in chosen
                     if any(str(p.get("symbol", "")).startswith(r["sym"])
                            for p in (tradier_orders.open_positions() or []))}
        except Exception:
            held |= {r["sym"] for r in chosen}     # fail closed

    logger.info("%s  budget $%.0f over %d trade(s) = $%.0f each  ranked by %s",
                now.strftime("%Y-%m-%d %H:%M %Z"), budget, len(chosen), per, args.by)
    placed = 0
    for r in chosen:
        sym, side, cost, w = r["sym"], r["_side"], float(r["cost"]), float(r["w"])
        long_k = r["_long"]
        short_k = float(r["hi"]) if long_k == float(r["lo"]) else float(r["lo"])
        qty = min(int(min(per, available) // (cost * 100)), tradier_orders.MAX_CONTRACTS)
        logger.info(
            "%-5s %-4s %.0f/%.0f w%.1f x%d @ %.2f = $%.0f | Pwin %.1f%% need %.1f%% "
            "EV $%+.0f | entry %.0f%% of width, extr %.0f%%, short %.2f ATR out, "
            "target %s %.2f (%.2f ATR) | news %s (%.2f) | macro %s (open %s)",
            sym, side.upper(), long_k, short_k, w, qty, cost, cost * 100 * qty,
            r["pwin"] * 100, r["need"] * 100, r["ev_dem"], r["_ew"] * 100,
            r["_ex_pct"], r["_short_atr"], sym, r["_target_spot"], r["_move_atr"],
            r.get("news") or "none", r.get("news_conf") or 0.0,
            mv or "none", mopen or "-")
        if qty < 1:
            logger.info("   costs $%.0f, above the $%.0f available (slot $%.0f, bucket/"
                        "buying power left $%.0f) — skipped.",
                        cost * 100, min(per, available), per, available)
            continue
        if sym in held:
            logger.info("   the account already holds %s options expiring %s "
                        "— skipped rather than doubled.", sym, exp)
            continue
        if not live:
            logger.info("   DRY RUN — not sent.")
            continue
        try:
            res = tradier_orders.submit_vertical(
                sym, exp, "call" if side == "call" else "put",
                long_strike=long_k, short_strike=short_k, quantity=qty,
                opening=True, limit_price=cost, is_credit=False)
            if (res or {}).get("status") == "refused":
                logger.warning("   NOT SENT — %s.", res.get("reason"))
                continue
            logger.info("   ORDER SENT: %s", res)
            placed += 1
            available = max(available - cost * 100 * qty, 0.0)
        except Exception:
            logger.exception("   order failed for %s — nothing opened.", sym)

    if live:
        logger.info("%d order(s) sent. Exits are orphans.py's job: -10%% stop, "
                    "+%.0f%% target, trail, 15:45 flatten.", placed, TARGET_PCT)
    else:
        logger.info("DRY RUN. Re-run with --live and TRADING_DTE0_LIVE=true to place.")


if __name__ == "__main__":
    main()
