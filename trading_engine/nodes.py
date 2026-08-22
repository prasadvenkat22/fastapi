"""The five trading graph agents.

macd_agent / sma_agent / bollinger_agent compute technical indicators from the
yfinance QQQ bar series. market_signals_agent combines Tradier breadth data,
VIX, scraped headlines, and a Claude structured-output sentiment call.
execution_risk_agent is the deterministic rule engine that turns all of the
above into a final trading decision.
"""

import logging
import os
from dataclasses import replace as _dc_replace
from datetime import datetime
from typing import List
from zoneinfo import ZoneInfo

import feedparser
import pandas as pd
from langchain_anthropic import ChatAnthropic

from GENAI.vector_stores import VoyageEmbeddings
from schemas_pgrs.trading_schema import MarketSentimentOutput

from .breadth_history import RECENT_WINDOW_MINUTES, record_and_summarize
from .equity import MAX_CONSECUTIVE_LOSSES, blocked_direction, consecutive_losses_today, current_equity
from .playbook import (
    CREDIT, credit_strikes_for, final_take_profit_for, ratchet_giveback_for,
    ride_deadline, rides_to_close, risk_share_for, strikes_for, thresholds_for,
    window_for,
)
from .broker import (
    BEAR_PUT_SPREAD,
    BULL_CALL_SPREAD,
    ITM_OFFSET,
    MockBrokerClient,
    default_mock_broker,
    is_credit,
    option_type_for,
    CALL_CREDIT_SPREAD,
    PUT_CREDIT_SPREAD,
    estimate_credit_value,
    estimate_spread_value,
    fill_price,
    minutes_to_expiry,
    round_to_strike,
)
NY = ZoneInfo("America/New_York")

from .data_feed import (fetch_market_breadth, fetch_qqq_bars, fetch_qqq_session_vwap,
                        fetch_oil, fetch_qqq_spot, fetch_tnx, fetch_vix,
                        chain_condor_value, chain_vertical, fetch_option_chain,
                        log_price_divergence, strike_for_delta, _regular_session_open)
from .state import TradingState

logger = logging.getLogger(__name__)

KILL_SWITCH_PATH = "KILL_SWITCH.txt"

# Debit-spread strategy config — see execution_risk_agent below.
POSITION_BUDGET = float(os.getenv("TRADING_POSITION_BUDGET", "1000"))

# Share of the budget the opening trade may consume. The remainder is held
# back to fund scale-ins.
#
# This has to be below 1.0 for scale-ins to exist at all. estimate_spread_
# quantity() buys as many contracts as the amount handed to it affords, so
# passing the whole budget left available_cash at exactly $0 on every entry
# — and the buy-more gate, which requires cash on hand, could therefore
# never pass at any budget. Rule 3 of the exit ladder was unreachable.
# 0.10, calibrated for the credit structure this playbook now trades.
#
# A credit vertical is sized on capital at RISK (width - credit, ~$260 a
# contract) rather than the premium collected, so 0.10 of a $10k book buys 3
# spreads and puts ~$780 -- 7.8% -- at risk in one position. Measured over 60
# sessions that is +57.04 a day against a 639 maximum drawdown, versus +14.13
# and 165 at 0.04. Doubling again to 0.20 roughly doubles the return and puts
# 18% of the account into a single 0DTE position, which is the wrong side of
# the trade for a structure whose losses are rare and large.
#
# Note if TRADING_ENABLED_WINDOWS is set back to ALL: at ~$183 a debit
# contract this same fraction buys 5, and one -30% stop is $274 against a
# $200 daily cap, so the cap halts the day on a single loss. Lower the
# fraction alongside re-enabling the debit windows.
ENTRY_FRACTION = float(os.getenv("TRADING_ENTRY_FRACTION", "0.10"))

# Share of the day's risk budget a window gets when it names none of its own,
# and the share a post-loss re-entry gets whatever window it is in.
#
# Re-entries are budgeted separately because they are a different trade: the
# tape has just disagreed with the setup. equity.REENTRY_COOLDOWN_MINUTES
# measured every extra shot after a loss as costing money on average (+51.17
# a day at no cooldown, +58.13 at thirty minutes, monotonic), so the shot
# that survives the cooldown gets the smaller allowance, not the larger one.
DEFAULT_RISK_SHARE = float(os.getenv("TRADING_DEFAULT_RISK_SHARE", "0.20"))
REENTRY_RISK_SHARE = float(os.getenv("TRADING_REENTRY_RISK_SHARE", "0.30"))

# Cap on scale-ins per position, unchanged from the original hardcoded 3.
# 0: scaling into a loser doubles the position on a thesis the market has
# already disproved. Left configurable, but off by default.
MAX_SCALE_INS = int(os.getenv("TRADING_MAX_SCALE_INS", "0"))

# Whether the RELAXED entry tier trades at all. Set false to fall back to the
# original strict gate everywhere.
RELAXED_ENTRIES_ENABLED = os.getenv("TRADING_RELAXED_ENTRIES", "true").lower() == "true"

# Whether the MOMENTUM tier trades: a 20-period midline cross with MACD and
# trend agreeing, catching moves that never stretch far enough to touch a band.
MOMENTUM_ENTRIES_ENABLED = os.getenv("TRADING_MOMENTUM_ENTRIES", "false").lower() == "true"

# Whether the TREND tier trades: MACD, trend and an RSI extreme agreeing, with
# NO Bollinger requirement.
#
# It exists because every other debit tier needs a band pierce, and Bollinger
# bands are computed from a rolling 100-minute mean -- so they drift down with
# a falling market and a steady decline never gets 2 sigma outside its own
# average. Bands catch dislocations, not trends.
#
# The cost was measured on a live session: QQQ fell $11 and from 10:03 the
# engine read BEARISH + BELOW_SMA + OVERSOLD -- a complete bearish setup --
# and refused it for twelve straight cycles because bollinger_zone was NORMAL.
# It took no trade all day. Over a month this tier fires 4.6 times a day and
# 77 of its 102 setups are ones no other tier catches.
TREND_ENTRIES_ENABLED = os.getenv("TRADING_TREND_ENTRIES", "false").lower() == "true"

# CLEAN: all four structural rules aligned -- price above/below the 20 SMA,
# the 9 EMA on the same side of it, price on the right side of VWAP, and RSI
# mid-band and still moving. The most selective tier and the only one using
# VWAP or an RSI band at all.
#
# Measured over a month: the state is true 9.3 bars a day, but as a TRIGGER
# (the first bar all four turn true) it fires 5.8 times, roughly twice what
# the market supplies. The RSI band does 75% of the filtering; VWAP only 16%.
CLEAN_ENTRIES_ENABLED = os.getenv("TRADING_CLEAN_ENTRIES", "true").lower() == "true"

# REJECT: a failed test of the 50 EMA from below. Bearish only -- the measured
# edge is one-directional and there is no evidence for a mirrored bullish case.
REJECT_ENTRIES_ENABLED = os.getenv("TRADING_REJECT_ENTRIES", "true").lower() == "true"

# Opening warmup. Entries wait this many minutes after the bell so the
# opening auction's whipsaws don't get read as a trend; position management
# is unaffected and runs from the first cycle.
MARKET_OPEN_HOUR, MARKET_OPEN_MINUTE = 9, 30
WARMUP_MINUTES = int(os.getenv("TRADING_WARMUP_MINUTES", "15"))

# Hard flatten time. A policy choice, not an expiry fact -- it trades the last
# of the day's theta convergence for distance from peak gamma and the widening
# quotes around the close. Set later to capture more of an ITM spread's
# convergence, earlier to sit further from the bell.
# 15:45 leaves half an hour of contract life (expiry is 16:15, see
# broker.EXPIRY_HOUR) -- enough that the model still prices real time value
# into the exit rather than marking to intrinsic on the way out.
_force_close_raw = os.getenv("TRADING_FORCE_CLOSE_TIME", "15:45")
try:
    FORCE_CLOSE_HOUR, FORCE_CLOSE_MINUTE = (int(p) for p in _force_close_raw.split(":"))
except ValueError:
    FORCE_CLOSE_HOUR, FORCE_CLOSE_MINUTE = 15, 45

# Bearish entries wait longer than bullish ones, and deliberately so. An
# opening reversal is not symmetric in cost: a long opened into a fading bounce
# bleeds, while a short opened into a V-shaped recovery is run over by the
# whole move. The opening auction produces exactly that shape often enough
# that the two directions do not deserve the same start time.
BEARISH_START_HOUR, BEARISH_START_MINUTE = (
    int(os.getenv("TRADING_BEARISH_START", "09:45").split(":")[0]),
    int(os.getenv("TRADING_BEARISH_START", "09:45").split(":")[1]),
)


def _is_before_bearish_start() -> bool:
    now_est = datetime.now(ZoneInfo("America/New_York"))
    return (now_est.hour, now_est.minute) < (BEARISH_START_HOUR, BEARISH_START_MINUTE)
# Once a position reaches its window's target it stops being a sell signal and
# becomes the point where a trailing exit ARMS. The trade then runs until the
# 5-minute trend breaks, so a strong move is not capped at the target.
#
# This matters because the cap was real: an ATM spread entered near $1.14 tops
# out around +163%, and booking it at +60% forfeits most of what the structure
# was chosen for. The tradeoff is that an armed winner can give back, so a
# floor is the 9 EMA itself.
TRAILING_EXITS_ENABLED = os.getenv("TRADING_TRAILING_EXITS", "true").lower() == "true"

# Once armed, give back at most this share of the best gain before closing.
# 0.20 means a position that peaked at +70% exits near +56% rather than
# riding back to the stop.
#
# The 9 EMA alone was not enough. It is a PRICE trail and knows nothing about
# P&L: spread value moves nonlinearly with price and time decay drains it
# independently, so a position can hand back most of its gain while price is
# still on the right side of the 9 EMA. Whichever triggers first wins.
TRAIL_GIVEBACK = float(os.getenv("TRADING_TRAIL_GIVEBACK", "0.20"))

# Profit level at which the ratchet starts protecting, INDEPENDENT of the
# take-profit target that arms the trailing exit.
#
# Tying the two together was wrong. A position peaking at +17% against a 40%
# target never armed, so nothing protected it -- observed live giving back
# +16.9% to +2.2% in three minutes with the ratchet dormant, free to continue
# to the -10% stop having been up 17%. Protecting a gain and deciding when to
# let a winner run are different questions and need different thresholds.
# 32, not 12: the floor this creates (arm minus giveback) has to clear the
# stop, or the ratchet books losers the stop would have caught anyway. At 32
# the floor is 22% against a -20% stop. At 12 it was +2%, which is inside the
# bid-ask and would have exited on quote noise.
RATCHET_ARM_PCT = float(os.getenv("TRADING_RATCHET_ARM_PCT", "32.0"))

# Smallest giveback that can trigger the ratchet, in points of return.
#
# The proportional giveback alone is too tight near the arm point: 15% of a
# 12% peak is 1.8 points, and the bid-ask round trip is already 2.8-4.2% of
# position value. The ratchet would fire on the quote oscillating rather than
# on the position actually turning, booking out of trades that never reversed.
# Whichever giveback is LARGER applies, so big winners still ratchet
# proportionally while small ones get room to breathe.
MIN_GIVEBACK_PCT = float(os.getenv("TRADING_MIN_GIVEBACK_PCT", "10.0"))

# How close spot must come to a credit spread's SHORT strike, in dollars,
# before the 9 EMA is allowed to end the ride.
#
# A debit spread needs direction, so price crossing the 9 EMA the wrong way
# is a real threat to it. A credit spread does not: it wins if the short
# strike holds, whichever way price wanders below it. Replayed on the
# 2026-08-20 trade, the ungated 9 EMA trail closed a short 714 call at +66%
# at 14:48 on a rally that stalled five dollars below the strike -- the
# position went on to +75%. Gated at two dollars it stayed open and the
# ratchet owned the exit, which is the rule that actually measures the
# position rather than the direction.
CREDIT_TRAIL_STRIKE_BUFFER = float(os.getenv("TRADING_CREDIT_TRAIL_BUFFER", "2.0"))

# Share of a debit spread's MAXIMUM profit at which a riding position books
# instead of running to the force close.
#
# A ride has no take-profit by design, because an ITM debit spread capped near
# +60% was giving up half its move to a +30% target. But the cap itself is a
# real number: a spread cannot be worth more than its width, so once it is
# within a tenth of that there is no move left to ride -- only the last few
# cents of convergence, held through the widest quotes of the day.
#
# Expressed as a share of max profit rather than a return, because the two
# differ per entry. A $5 spread bought at $3.23 has $1.77 of profit in it, so
# the ceiling is +49% of premium paid; bought at $2.50 the same 90% ceiling is
# +90%. The number that stays constant across entries is the share.
RIDE_CEILING_FRACTION = float(os.getenv("TRADING_RIDE_CEILING", "0.90"))

# Profit protection for a RIDING position: arm at this return, then close if
# it hands back more than the giveback below.
#
# A ride deliberately has no take-profit, and until now it had nothing else
# either -- between the stop and the ceiling there was no rung at all. The
# engine-wide ratchet lives in a branch a riding position never reaches, and
# its 32% arm sits above where these trades actually peak.
#
# Observed live on 2026-08-21: the morning spread peaked at +26.4% as QQQ hit
# 715 at 11:52 and closed at the 13:25 handoff for +16.7%, handing back 9.7
# points with no rule watching. That is the same hole RATCHET_ARM_PCT was
# written to close for non-riding positions.
#
# Separate giveback knobs because the engine-wide MIN_GIVEBACK_PCT of 10
# points is most of a 26-point peak -- it would have fired at +16.4%, which
# is where the handoff landed anyway. Zero disables the rung entirely.
RIDE_RATCHET_ARM_PCT = float(os.getenv("TRADING_RIDE_RATCHET_ARM", "0"))
RIDE_GIVEBACK = float(os.getenv("TRADING_RIDE_GIVEBACK", "0.20"))
RIDE_MIN_GIVEBACK_PCT = float(os.getenv("TRADING_RIDE_MIN_GIVEBACK", "5.0"))

# Least credit worth selling a spread for, per contract.
#
# Without a floor the engine will open a credit vertical that collects
# nothing. Sizing does not stop it -- estimate_credit_quantity divides the
# budget by width-minus-credit, which is at its LARGEST when the credit is
# zero, so a worthless entry sizes like a normal one. return_pct then reads
# 0.0 forever (it guards its own denominator), so the position can never take
# profit and never stop out; it just holds full width of downside to the
# force close for an upside of nothing.
#
# Found by scripts/sweep.py replaying 60 sessions: on quiet days the model
# prices a 3-sigma-out spread at zero and the engine takes it.
MIN_CREDIT = float(os.getenv("TRADING_MIN_CREDIT", "0.05"))

# Target delta for a credit spread's SHORT strike, when the chain is
# available to measure it.
#
# The volatility placement it replaces put the short strike three standard
# deviations out, which sounds conservative and on a quiet afternoon is
# simply too far to be worth selling. Measured live on 2026-08-21: it chose
# the 716 call with spot near 710, a 7-delta strike the market valued at
# $0.02 the spread. The engine believed it had collected $0.28 because the
# model said so, and booked a profit on premium no one would have paid.
#
# 0.20 is the conventional short-strike delta for a credit vertical: far
# enough that it expires worthless most days, close enough to be worth
# selling. Sigma placement remains the fallback when the chain is missing.
CREDIT_SHORT_DELTA = float(os.getenv("TRADING_CREDIT_SHORT_DELTA", "0.20"))

# Refuse BULLISH entries once the session itself is down this far, in percent
# from the regular-hours open. Zero disables the filter.
#
# Every trend reading the engine had was intraday and short -- a 20 and 50 EMA
# over five-minute bars, a VWAP, a nine-period EMA. On a day that opens bad
# and keeps going, an ordinary bounce lifts price above all of them while the
# session is still deeply red, and the tier ladder reads that as a clean
# bullish stack.
#
# Measured across 60 sessions bucketed by the 09:45-to-close move, hard-down
# days (worse than -0.75%) are the engine's only losing regime at -47.46 a
# day, and the trades it took there include put credit spreads and a call
# debit spread -- bullish structures, sold into a decline that continued.
# Every other bucket is positive: -0.75..-0.25 +6.54, flat +70.26, up +68.54,
# hard up +130.37.
DAY_TREND_MAX_DROP_PCT = float(os.getenv("TRADING_DAY_TREND_MAX_DROP", "0"))

# On a STRONG trend, place the long leg this many dollars in the money
# instead of the window's usual depth. Zero disables it.
#
# A deep spread caps early by construction. On 2026-08-21 the morning trade
# was long 707 / short 712 with QQQ at 711.88; price ran to 714.82 and every
# cent above 712 paid nothing, because the structure was already at maximum
# intrinsic. Trading the same day with the long leg $2 in the money would
# have put the short strike at 714 and left room for the rest of the move.
#
# The reason it is not simply done that way is measured: a $2-deep long leg
# wins 31% of the time against 56% for the full-width placement, and the $5
# shallow variant lost money outright. This asks the narrower question --
# whether a shallow placement pays when ADX says the trend is real, which is
# the only condition under which its lower hit rate could be worth the higher
# ceiling.
TRENDING_LONG_DEPTH = float(os.getenv("TRADING_TRENDING_LONG_DEPTH", "0"))
TRENDING_ADX_MIN = float(os.getenv("TRADING_TRENDING_ADX_MIN", "25"))

# Most of equity ONE position may put at structural risk -- not at stop risk.
#
# Every other control in this engine governs the loss the rules intend: the
# stop, the risk share, the daily cap. None of them governs the loss the
# market can impose. A credit spread's stop is a percentage of the credit
# collected, perhaps $59 a contract; its structural maximum is width minus
# credit, $341 a contract, and a gap through both strikes pays the second
# number, not the first. The daily cap does not help -- it halts new entries
# after realised losses and cannot close the distance on a position already
# open.
#
# That gap is invisible while size is small and is the whole story once size
# is the thing being raised. Swept over 60 sessions, daily P&L scales almost
# perfectly linearly with the entry fraction (+71.53/day at 10%, +225.44 at
# 30%) precisely because those sessions contained no gap -- the short strike
# held in 89% of them and the stop caught the rest. Sizing off that curve is
# sizing off a sample that never met the risk being taken.
#
# 0.15: one position may put 15% of the account at structural risk. At
# today's equity that is four credit contracts, and it is the constraint that
# binds rather than the capital fraction.
MAX_POSITION_RISK_PCT = float(os.getenv("TRADING_MAX_POSITION_RISK_PCT", "0.15"))

# Let a credit window open without a directional tier, taking its side from
# the trend alone.
#
# The tier ladder was built for debit spreads, which need a move to pay and
# so need a directional read worth acting on. A credit spread does not: it
# pays if its short strike holds, and the strike holding is mostly a question
# of distance and time, not direction. Measured model-free over 60 sessions,
# a call short four dollars above spot at 13:30 is never touched in 92% of
# them -- with no signal required at all.
#
# What the gate costs is window time. On 2026-08-21 the credit window opened
# at 13:30 and the tier did not line up until 14:29, so the trade collected
# an hour less decay than it could have.
CREDIT_LOOSE_GATE = os.getenv("TRADING_CREDIT_LOOSE_GATE", "false").lower() == "true"

# A debit spread cannot be worth more than its width, so the most a position
# can ever gain is (width - entry debit) / entry debit — and the entry debit
# rises through the day as time value drains, which lowers that ceiling as
# the session runs on. Measured on a $3-wide spread: ~62% at the open, ~49%
# by 13:00, ~42% by the 14:00 cutoff. 30% stays reachable at any entry time;
# 50% is structurally impossible after midday, and a target that can't be hit
# isn't a target — the position just rides to the force-close instead.
TAKE_PROFIT_PCT = float(os.getenv("TRADING_TAKE_PROFIT_PCT", "30.0"))
# -20, measured rather than chosen: the bid-ask round trip alone is 5.2-5.8%
# of these positions and one median 5-minute bar moves an ITM 3-wide about
# 10%, so a -10% stop fires on ordinary noise. Per-window overrides in
# playbook.py widen this further where the volatility regime demands it.
STOP_LOSS_PCT = float(os.getenv("TRADING_STOP_LOSS_PCT", "-20.0"))

# Tightened stop for LONG positions while macro is risk-off (market_sentiment
# == 'BAD'). Riding a losing 0DTE spread all the way to the full stop
# into a deteriorating tape gives up twice the capital for a position whose
# thesis has already broken; cutting earlier and re-entering later if
# conditions improve is the cheaper path — entries are re-evaluated every
# scheduler cycle anyway, so nothing is permanently forfeited by leaving.
RISK_OFF_STOP_LOSS_PCT = float(os.getenv("TRADING_RISK_OFF_STOP_LOSS_PCT", "-13.0"))

# Macro risk-off thresholds. VIX is judged on both level and session move:
# a spike of this magnitude is treated as risk-off even from a low base.
VIX_LEVEL_MAX = float(os.getenv("TRADING_VIX_LEVEL_MAX", "22.0"))
VIX_SPIKE_PCT = float(os.getenv("TRADING_VIX_SPIKE_PCT", "10.0"))

# 10-year Treasury yield, judged purely on intraday velocity. The Nasdaq-100
# is the longest-duration equity index, so its multiple moves inversely with
# real rates — a sharp yield spike is a direct headwind that neither VIX nor
# breadth necessarily shows. A typical session moves the 10Y 3-6bp; 8bp is a
# real move against a long tech position.
#
# One-sided on purpose. Rising yields hurt QQQ; falling yields are broadly
# supportive of it, and the flight-to-quality case where yields collapse in a
# crash arrives with a VIX spike that the gate above already catches.
#
# 4bp, not the 8bp this shipped with. Measured against a month of 5-minute
# history, the 10Y's intraday peak never exceeded 4.3bp on any session -- an
# 8bp gate could not fire and never did. The threshold was set from a guess
# about what a "real move" looks like on a daily chart, not from what the
# instrument actually does inside a session.
#
# 4bp is rare but real: 3 of 22 sessions reached it, and QQQ finished down on
# all three (-0.89%, -0.59%, -0.30%). Three days is far too small to call an
# edge -- it is enough to say the gate can now fire at all, which is the
# precondition for ever learning whether it should.
TNX_SPIKE_BPS = float(os.getenv("TRADING_TNX_SPIKE_BPS", "4.0"))

# Breadth is judged on level and trend alike. Expressed as a drop in net
# breadth ratio (advancers-minus-decliners over basket size) from its peak
# over the recent window: 0.40 is roughly a fifth of the basket flipping
# from advancing to declining inside half an hour — participation draining
# out of a tape that still prints positive.
BREADTH_COLLAPSE_RATIO = float(os.getenv("TRADING_BREADTH_COLLAPSE_RATIO", "0.40"))

# Standard Wilder's RSI(14) thresholds.
RSI_OVERBOUGHT = float(os.getenv("TRADING_RSI_OVERBOUGHT", "70.0"))
RSI_OVERSOLD = float(os.getenv("TRADING_RSI_OVERSOLD", "30.0"))

# Trend-entry RSI bands: strength present, not yet exhausted. Deliberately
# mid-range rather than extreme -- the opposite construction to the
# overbought/oversold gates, which look for a snap-back.
RSI_BULL_BAND = (float(os.getenv("TRADING_RSI_BULL_LOW", "50.0")),
                 float(os.getenv("TRADING_RSI_BULL_HIGH", "65.0")))
RSI_BEAR_BAND = (float(os.getenv("TRADING_RSI_BEAR_LOW", "35.0")),
                 float(os.getenv("TRADING_RSI_BEAR_HIGH", "48.0")))

# How long the headline read (RSS scrape + Voyage embedding + Claude verdict)
# is reused before being refreshed. The deterministic gates -- breadth, VIX,
# TNX -- always recompute, because those are the velocity signals that exist
# precisely to catch sudden change.
#
# This exists so the cycle interval and the macro cost can move independently.
# At a 1-minute cadence an uncached macro pass would mean ~390 Claude calls
# and 1,170 RSS fetches per session, for a qualitative read that does not
# meaningfully change minute to minute.
MACRO_REFRESH_MINUTES = float(os.getenv("TRADING_MACRO_REFRESH_MINUTES", "5"))

def _record_macro_reading(vix, tnx) -> None:
    """Persist the VIX and yield readings this cycle gated on.

    Best-effort: losing a reading is not worth failing a trading cycle over.
    """
    from models_pgdb.trading_models import MacroReading
    from config.db_pgrs import SessionLocal
    try:
        db = SessionLocal()
        try:
            db.add(MacroReading(
                vix_level=vix.level, vix_session_open=vix.session_open,
                vix_change_pct=vix.change_pct,
                tnx_level=tnx.level, tnx_session_open=tnx.session_open,
                tnx_change_bps=tnx.change_bps,
            ))
            db.commit()
        finally:
            db.close()
    except Exception:
        logger.exception("Macro reading not recorded.")


def _read_macro_cache():
    """Last macro read, or None if absent or stale.

    Stored in Postgres rather than a module global. The global only worked
    because the in-app scheduler kept one process alive; under cron each run
    is a fresh process, so it never hit and every cycle paid for three RSS
    scrapes, two Voyage embeddings and a Claude call.
    """
    from datetime import timezone
    from models_pgdb.trading_models import MacroCache
    from config.db_pgrs import SessionLocal
    try:
        db = SessionLocal()
        try:
            row = db.query(MacroCache).filter(MacroCache.id == 1).first()
            if row is None or row.updated_at is None:
                return None
            age = (datetime.now(timezone.utc) - row.updated_at).total_seconds()
            if age >= MACRO_REFRESH_MINUTES * 60:
                return None
            return row.verdict, row.confidence, row.risk_factor
        finally:
            db.close()
    except Exception:
        logger.exception("Macro cache unreadable — falling back to a fresh read.")
        return None


def _write_macro_cache(verdict: str, confidence: float, risk_factor: str) -> None:
    from sqlalchemy.sql import func as sqlfunc
    from models_pgdb.trading_models import MacroCache
    from config.db_pgrs import SessionLocal
    try:
        db = SessionLocal()
        try:
            row = db.query(MacroCache).filter(MacroCache.id == 1).first()
            if row is None:
                db.add(MacroCache(id=1, verdict=verdict, confidence=confidence,
                                  risk_factor=risk_factor, updated_at=sqlfunc.now()))
            else:
                row.verdict, row.confidence = verdict, confidence
                row.risk_factor, row.updated_at = risk_factor, sqlfunc.now()
            db.commit()
        finally:
            db.close()
    except Exception:
        # Non-fatal: a failed write just means the next cycle recomputes.
        logger.exception("Macro cache write failed.")

RSS_FEEDS = [
    "https://finance.yahoo.com/news/rssindex",
    "https://feeds.content.dowjones.io/public/rss/mw_marketpulse",
    "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114",
]

# ---------------------------------------------------------------------------
# Technical indicator agents
# ---------------------------------------------------------------------------


def macd_agent(state: TradingState) -> dict:
    """Standard 12/26/9 MACD over the fetched intraday bar series."""
    bars = fetch_qqq_bars()
    close = bars["Close"]

    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd_line = ema12 - ema26
    signal_line = macd_line.ewm(span=9, adjust=False).mean()
    histogram = macd_line - signal_line

    latest = histogram.iloc[-1]
    if latest > 0.01:
        signal = "BULLISH"
    elif latest < -0.01:
        signal = "BEARISH"
    else:
        signal = "NEUTRAL"

    # Recorded on every cycle, not just entries: without it a HOLD cycle
    # leaves no trace of where price was, and "what did we miss" cannot be
    # answered from our own data afterwards.
    return {"macd_signal": signal, "qqq_close": round(float(close.iloc[-1]), 2)}


def sma_agent(state: TradingState) -> dict:
    """Closing price vs. the 20- and 50-period EMAs (computed over the
    fetched intraday bar series). Uses EMA rather than a simple moving
    average so this trend filter reacts on the same timescale as MACD
    (which is itself EMA(12,26,9)-based) — important for same-day (0DTE)
    positions where a laggy SMA can confirm a trend after most of the day's
    move is already gone. The state field/values (sma_trend,
    ABOVE_SMA/BELOW_SMA) are kept as-is; only the underlying average changed."""
    bars = fetch_qqq_bars()
    close = bars["Close"]

    ema20 = close.ewm(span=20, adjust=False).mean().iloc[-1]
    ema50 = close.ewm(span=50, adjust=False).mean().iloc[-1] if len(close) >= 50 else ema20
    last_close = close.iloc[-1]

    trend = "ABOVE_SMA" if (last_close > ema20 and last_close > ema50) else "BELOW_SMA"

    # 9 EMA is the trailing reference, not an entry filter. A winning trade is
    # held while price keeps closing on the right side of it, which is what
    # lets a run go past any fixed target.
    # 50 EMA rejection: this bar's HIGH pokes above the 50 EMA but the CLOSE
    # finishes below it — buyers tried the ceiling and failed.
    #
    # Measured over a month of 5-minute bars: 96 occurrences, QQQ lower 66% of
    # the time afterwards, averaging -$0.31 over the next 3 bars and -$0.66
    # over 6. Baseline for all bars is 50% and roughly zero drift, and merely
    # being below the 50 EMA is only 53%. The rejection itself carries the
    # signal, not the position.
    ema50_series = close.ewm(span=50, adjust=False).mean()
    ema50_last = ema50_series.iloc[-1]
    ema50_reject = bool(bars["High"].iloc[-1] > ema50_last and last_close < ema50_last)

    ema9_series = close.ewm(span=9, adjust=False).mean()
    ema9 = ema9_series.iloc[-1]
    ema9_side = "ABOVE_EMA9" if last_close > ema9 else "BELOW_EMA9"

    # Rule B proper: 9 EMA vs the 20 SMA, not price vs the 9 EMA. The first
    # says velocity is accelerating in the trend's direction; the second only
    # says price is above a fast average. They agree often and not always.
    sma20 = close.rolling(20).mean().iloc[-1]
    ema_cross = "EMA9_ABOVE_SMA20" if ema9 > sma20 else "EMA9_BELOW_SMA20"

    # Rule C: VWAP, the institutional anchor. Must reset each session — a
    # VWAP carried across days is not VWAP, it is a slow moving average.
    vwap = fetch_qqq_session_vwap()
    vwap_side = "UNKNOWN" if vwap is None else ("ABOVE_VWAP" if last_close > vwap else "BELOW_VWAP")

    # Trend STRENGTH, alongside the direction read above. Recorded only — see
    # ADX_TREND_THRESHOLD for why it gates nothing.
    adx = compute_adx(bars)
    adx_ok = adx == adx  # False for NaN
    adx_zone = (
        "UNKNOWN" if not adx_ok
        else ("TRENDING" if adx >= ADX_TREND_THRESHOLD else "CHOPPY")
    )

    # Where the session stands, as distinct from where the last few bars do.
    # The EMAs this function returns are intraday and short: on a day that
    # falls all morning, an ordinary bounce lifts price above both of them
    # while the session is still deeply red. That is how bullish entries were
    # reaching hard-down days -- see DAY_TREND_MAX_DROP_PCT below.
    try:
        session_open = _regular_session_open(bars)
        session_move_pct = (
            (float(last_close) - session_open) / session_open * 100.0 if session_open else 0.0
        )
    except Exception:
        session_move_pct = 0.0

    return {"sma_trend": trend, "ema9_side": ema9_side, "ema_cross": ema_cross,
            "vwap_side": vwap_side, "ema50_reject": ema50_reject,
            "session_move_pct": round(session_move_pct, 3),
            "adx": round(adx, 2) if adx_ok else 0.0, "adx_zone": adx_zone}


ADX_PERIOD = int(os.getenv("TRADING_ADX_PERIOD", "14"))
# Conventional trending/choppy line. Recorded only -- ADX does not gate any
# entry, because measured over 60 sessions it did not earn one.
#
# On the morning debit leg a 22 threshold discriminated nothing: +4.50 a trade
# above it against +4.03 below. On the credit leg it was informative but
# BACKWARDS from the usual advice -- selling premium returned +63.39 a trade at
# ADX >= 22 and +35.28 below it, every split stable across sample halves. High
# ADX means high volatility, which widens our 3-sigma strike placement and
# fattens the credit at the same time: further away and paid more. Gating
# credit to quiet tape would skip the best trades.
#
# Logged so the question can be revisited on our own forward data.
ADX_TREND_THRESHOLD = float(os.getenv("TRADING_ADX_TREND_THRESHOLD", "22.0"))


def _wilder(series, period: int):
    """Wilder's smoothing — an EMA with alpha = 1/period."""
    return series.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()


def compute_adx(bars, period: int = ADX_PERIOD) -> float:
    """Wilder's ADX over the fetched bar series, or NaN if too short.

    Measures how strongly price is trending without saying which way — the
    directional read stays with the MA/VWAP stack in sma_agent.
    """
    high, low, close = bars["High"], bars["Low"], bars["Close"]
    prev_close, prev_high, prev_low = close.shift(1), high.shift(1), low.shift(1)

    true_range = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)

    up_move, down_move = high - prev_high, prev_low - low
    plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
    minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)

    atr = _wilder(true_range, period)
    plus_di = 100.0 * _wilder(plus_dm, period) / atr
    minus_di = 100.0 * _wilder(minus_dm, period) / atr

    total = (plus_di + minus_di).replace(0.0, float("nan"))
    dx = 100.0 * (plus_di - minus_di).abs() / total
    adx = _wilder(dx, period)
    return float(adx.iloc[-1]) if len(adx) and adx.iloc[-1] == adx.iloc[-1] else float("nan")


# Iron Condor shadow log. Notionally opens a condor at these times each day
# and marks it every cycle, WITHOUT trading it.
#
# Measured over 60 sessions the structure lost money at every parameter
# combination tried -- 23 of them, across two entry times, four wing
# distances, two profit targets and two stop multiples. At its own 09:45
# entry the best cell was -41.36 a condor and the worst -53.10, against the
# +61.00 at an 85% win rate that the existing 13:30 credit trade made on the
# same sideways sessions.
#
# That is enough to refuse to trade it and not enough to close the question:
# it rests on vendor bars and our own pricing model. So the condor is marked
# and written to the cycle log instead, building a forward out-of-sample
# record at no risk. If it earns its place over a few weeks, the schema work
# is justified then.
# 13:30 added after the 2026-08-21 sweep. The two morning entries were chosen
# when the question was "does the 09:45 condor the notes recommend work" --
# it does not, at -0.32 to +3.66 a condor with halves that flip sign. The
# same sweep found the 13:30 cell is the one worth watching: +38.95 a condor
# unfiltered and +54.62 behind ADX<22, at 82-88% win rates with halves that
# agree. The shadow log has to cover the promising cell, not just the
# rejected one, or the forward record answers a question nobody is asking.
SHADOW_CONDOR_ENTRIES = ((9, 45), (10, 15), (13, 30))
# Wings a FIXED distance out, not a sigma multiple. The live credit window
# places its short strike at 3 sigma, which is right for a single wing sold
# at 13:30 -- but applied to a 09:45 condor it put the wings 12 points out
# and collected $0.10 the pair. The strategy being evaluated collects
# $80-110, so a sigma-placed log would be evidence about a different trade.
# $4 is the closest match to its 0.15-delta short strike and was the best
# of the distances measured.
SHADOW_CONDOR_OFFSET = float(os.getenv("TRADING_SHADOW_CONDOR_OFFSET", "4.0"))
CONDOR_WIDTH = float(os.getenv("TRADING_SHADOW_CONDOR_WIDTH", "3.0"))


def shadow_condor_marks(bars, spot: float) -> dict:
    """Mark condors notionally opened earlier today, from the bar series alone.

    Deliberately stateless — it reconstructs the entry from the historical
    bar at each entry time rather than remembering anything, so it cannot
    drift out of sync with the live position and cannot influence a decision.
    """
    marks: dict = {}
    try:
        idx = bars.index
        if getattr(idx, "tz", None) is not None:
            idx = idx.tz_convert(NY)
        else:
            return marks
        today = datetime.now(NY).date()
        closes = bars["Close"]
        sd20 = closes.rolling(20).std()

        for hour, minute in SHADOW_CONDOR_ENTRIES:
            key = "condor_%02d%02d" % (hour, minute)
            hits = [
                i for i, ts in enumerate(idx)
                if ts.date() == today and (ts.hour, ts.minute) == (hour, minute)
            ]
            if not hits:
                continue
            pos = hits[0]
            entry_spot = float(closes.iloc[pos])
            sd = sd20.iloc[pos]
            offset = SHADOW_CONDOR_OFFSET
            atm = round_to_strike(entry_spot)
            call_short, call_long = atm + offset, atm + offset + CONDOR_WIDTH
            put_short, put_long = atm - offset, atm - offset - CONDOR_WIDTH

            entry_minutes = minutes_to_expiry(idx[pos].to_pydatetime())
            credit = (
                fill_price(estimate_credit_value(CALL_CREDIT_SPREAD, call_short, call_long,
                                                 entry_spot, entry_minutes), "sell")
                + fill_price(estimate_credit_value(PUT_CREDIT_SPREAD, put_short, put_long,
                                                   entry_spot, entry_minutes), "sell")
            )
            if credit <= 0.02:
                continue
            cost = (
                fill_price(estimate_credit_value(CALL_CREDIT_SPREAD, call_short, call_long, spot), "buy")
                + fill_price(estimate_credit_value(PUT_CREDIT_SPREAD, put_short, put_long, spot), "buy")
            )
            # The same mark, at real quotes. Everything above is the model
            # marking its own homework: it produced the entry credit and it
            # produces the value now, so a condor can look like a 94% winner
            # without a market ever having offered either price. The chain
            # says what closing it actually costs, and the four-leg bid-ask
            # width says whether a 90%-decay exit is reachable at all -- on
            # the quotes measured so far it is $9-11 a block against a target
            # of $6-13, which is most or all of the last tenth.
            market = None
            try:
                market = chain_condor_value(call_short, call_short + CONDOR_WIDTH,
                                            put_short, put_short - CONDOR_WIDTH)
            except Exception:
                logger.exception("Chain condor mark failed — model mark only.")

            # The market's price for this condor AT ENTRY, logged in the
            # minutes around the entry bar. The mark is stateless and
            # recomputed from bars every cycle, so it can never recover a
            # quote from earlier in the day -- and without a real entry
            # credit the whole forward record is model fiction, which is the
            # error this file just spent a session correcting elsewhere.
            try:
                age_min = abs((datetime.now(NY) - idx[pos].to_pydatetime()).total_seconds()) / 60.0
                if age_min <= 6.0 and market is not None:
                    logger.info(
                        "Shadow condor %s ENTRY at market: credit %.3f mid / %.3f natural "
                        "(model says %.3f), four-leg width %.3f, short deltas %s",
                        key, market["mid"], market["natural"], credit,
                        market["spread_width"], market["short_deltas"],
                    )
            except Exception:
                pass

            marks[key] = {
                "entry_spot": round(entry_spot, 2),
                "entry_sd20": round(float(sd), 4) if sd == sd else None,
                "call_short": call_short, "put_short": put_short,
                "width": CONDOR_WIDTH,
                "credit": round(credit, 4),
                "value_now": round(cost, 4),
                "return_pct": round((credit - cost) / credit * 100.0, 2),
                "breached": bool(spot >= call_short or spot <= put_short),
                # Market marks, None when the chain is unavailable or the
                # strikes are not listed. return_pct_market uses the model's
                # entry credit -- the snapshot cannot recover a price from
                # 09:45 -- so it isolates the exit side of the question.
                "market_value_now": market["mid"] if market else None,
                "market_natural_cost": market["natural"] if market else None,
                "market_spread_width": market["spread_width"] if market else None,
                "market_short_deltas": market["short_deltas"] if market else None,
                "return_pct_market": (
                    round((credit - market["mid"]) / credit * 100.0, 2)
                    if market and credit else None
                ),
            }
    except Exception:
        # Never let an observability feature break a trading cycle.
        logger.exception("Shadow condor marking failed — continuing without it.")
    return marks


def bollinger_agent(state: TradingState) -> dict:
    """20-period Bollinger Bands (2 standard deviations)."""
    bars = fetch_qqq_bars()
    close = bars["Close"]

    window = close.rolling(window=20)
    mid = window.mean().iloc[-1]
    std = window.std().iloc[-1]
    last_close = close.iloc[-1]

    upper = mid + (2 * std)
    lower = mid - (2 * std)

    if last_close >= upper:
        zone = "UPPER_BAND"
    elif last_close <= lower:
        zone = "LOWER_BAND"
    else:
        zone = "NORMAL"

    # Midline cross: price closing through its 20-period mean. A different
    # thesis from a band pierce -- the band says "stretched", the midline says
    # "trend just turned" -- which is why the momentum tier uses it.
    prev_close = close.iloc[-2] if len(close) >= 2 else last_close
    prev_mid = window.mean().iloc[-2] if len(close) >= 2 else mid
    if last_close > mid and prev_close <= prev_mid:
        cross = "CROSS_UP"
    elif last_close < mid and prev_close >= prev_mid:
        cross = "CROSS_DOWN"
    else:
        cross = "NONE"

    # Observability only — a condor we do NOT trade, marked so a forward
    # record accumulates. See shadow_condor_marks for why it is not traded.
    shadow = shadow_condor_marks(bars, float(last_close))

    return {"bollinger_zone": zone, "bollinger_cross": cross,
            "bollinger_sd": round(float(std), 4) if std == std else 0.0,
            "shadow_condor": shadow}


def rsi_agent(state: TradingState) -> dict:
    """Standard 14-period RSI (Wilder's smoothing) over the fetched intraday
    bar series. >=70 overbought (fading a stretched high — pairs with
    Bollinger UPPER_BAND for the bearish trigger), <=30 oversold (bouncing
    off a stretched low — pairs with Bollinger LOWER_BAND for the bullish
    trigger)."""
    bars = fetch_qqq_bars()
    close = bars["Close"]

    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()

    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    latest = rsi.iloc[-1]

    if latest >= RSI_OVERBOUGHT:
        zone = "OVERBOUGHT"
    elif latest <= RSI_OVERSOLD:
        zone = "OVERSOLD"
    else:
        zone = "NEUTRAL"

    # A separate reading for trend entries: strength present but not spent.
    # The extremes above are a mean-reversion idea -- price stretched far
    # enough to snap back. This is the opposite: mid-range and still moving,
    # which is what a trend looks like before it is exhausted. Measured over a
    # month this band did 75% of the filtering in the four-rule gate.
    prev = rsi.iloc[-2] if len(rsi) >= 2 else latest
    rising, falling = latest > prev, latest < prev
    if RSI_BULL_BAND[0] <= latest <= RSI_BULL_BAND[1] and rising:
        band = "BULL_BAND"
    elif RSI_BEAR_BAND[0] <= latest <= RSI_BEAR_BAND[1] and falling:
        band = "BEAR_BAND"
    else:
        band = "NONE"

    return {"rsi_zone": zone, "rsi_band": band}


# ---------------------------------------------------------------------------
# Market sentiment agent
# ---------------------------------------------------------------------------


def _scrape_headlines() -> List[str]:
    headlines: List[str] = []
    for url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
            headlines.extend(entry.title for entry in feed.entries[:10] if getattr(entry, "title", None))
        except Exception as e:
            logger.warning("Failed to parse RSS feed %s: %s", url, e)
    return headlines


async def market_signals_agent(state: TradingState) -> dict:
    from .vector_store import query_similar_headlines, store_headlines  # local import avoids a circular import with graph wiring

    breadth = await fetch_market_breadth()
    breadth_trend = record_and_summarize(breadth)
    vix = fetch_vix()
    tnx = fetch_tnx()
    # Crude is fetched and REPORTED but does not gate anything yet. Every
    # other macro term here was threshold-tuned against measured outcomes;
    # this one has no such history, and wiring an untested input into the
    # sentiment that gates risk-off exits would change live behaviour on a
    # guess. Logged now so a threshold can be set from our own data.
    try:
        oil = fetch_oil()
    except Exception:
        logger.exception('Crude fetch failed — continuing without it.')
        oil = None
    _record_macro_reading(vix, tnx)
    # The headline read is the expensive half of this agent and the half that
    # does not change minute to minute, so it is refreshed on its own clock.
    now = datetime.now(ZoneInfo("America/New_York"))
    cached = _read_macro_cache()
    cache_fresh = cached is not None

    headlines: List[str] = []
    similar_past_headlines: List[str] = []
    if not cache_fresh:
        headlines = _scrape_headlines()
        embeddings = VoyageEmbeddings()
        if headlines:
            await store_headlines(headlines, embeddings)
        similar_past_headlines = await query_similar_headlines(headlines, embeddings, top_k=3) if headlines else []

    # $TICKQ is intentionally not used — confirmed live against both Tradier
    # sandbox and production that this symbol doesn't exist in their catalog,
    # and there's no honest free approximation for a real tick index (needs
    # tick-by-tick trade data no snapshot-quote API provides). The
    # Institutional Divergence Filter that depended on it is dropped for now.
    # $ADDQ is self-computed from NASDAQ_BREADTH_BASKET (see data_feed.py):
    # net advancers/decliners > 0 (more names advancing than declining) is
    # bullish breadth, <= 0 is bearish. That level check is necessary but not
    # sufficient — breadth that peaked at +45 and has bled down to +3 still
    # satisfies it while describing a market losing participation by the
    # minute, so a drawdown check against the recent window's peak runs
    # alongside it (see breadth_history.py for why breadth needs persistence
    # to know this and VIX doesn't, and why the window is rolling rather than
    # session-anchored).
    breadth_is_collapsing = breadth_trend.drawdown_from_recent_peak <= -BREADTH_COLLAPSE_RATIO
    breadth_is_bullish = breadth.addq > 0 and not breadth_is_collapsing

    if breadth_is_collapsing:
        logger.warning(
            "Breadth collapsing: net ratio %.2f, down %.2f from the recent peak of %.2f (%d readings today) — forcing risk-off.",
            breadth_trend.net_ratio, breadth_trend.drawdown_from_recent_peak,
            breadth_trend.recent_peak_ratio, breadth_trend.reading_count,
        )

    llm = None if cache_fresh else ChatAnthropic(model="claude-opus-5", max_tokens=1024).with_structured_output(MarketSentimentOutput)
    prompt = (
        "You are a macro risk classifier for a same-day QQQ options trading system. "
        "Classify today's market risk as GOOD (safe to hold/enter a bullish position) or BAD (risk-off).\n\n"
        f"Nasdaq breadth (self-computed from a {breadth.basket_size}-stock basket): "
        f"{breadth.advancers} advancing, {breadth.decliners} declining, {breadth.unchanged} unchanged "
        f"(net {breadth.addq:+.0f})\n"
        + (
            f"Breadth trend today: net ratio {breadth_trend.net_ratio:+.2f}, "
            f"{breadth_trend.change_from_open:+.2f} from the open, "
            f"{breadth_trend.drawdown_from_session_peak:+.2f} from today's peak, "
            f"{breadth_trend.drawdown_from_recent_peak:+.2f} from the last "
            f"{RECENT_WINDOW_MINUTES:.0f} minutes' peak "
            f"(across {breadth_trend.reading_count} readings). A large drop from today's peak "
            f"with only a small recent drop is a slow all-day bleed rather than a sudden break.\n"
            if breadth_trend.has_history
            else "Breadth trend today: first reading of the session — no trend yet\n"
        )
        + f"CBOE Volatility Index (VIX): {vix.level:.2f} "
        + f"({vix.change_pct:+.1f}% from today's open of {vix.session_open:.2f})\n\n"
        f"Today's headlines:\n" + "\n".join(f"- {h}" for h in headlines[:20]) + "\n\n"
        + (
            "Similar historical headlines and what followed (for context):\n"
            + "\n".join(f"- {h}" for h in similar_past_headlines)
            if similar_past_headlines else ""
        )
    )
    if cache_fresh:
        llm_verdict, llm_confidence, llm_risk_factor = cached
    else:
        llm_result: MarketSentimentOutput = await llm.ainvoke(prompt)
        llm_verdict = llm_result.verdict
        llm_confidence = llm_result.confidence_score
        llm_risk_factor = llm_result.risk_factor
        logger.info("Macro verdict %s (confidence %.2f): %s", llm_verdict, llm_confidence, llm_risk_factor)
        _write_macro_cache(llm_verdict, llm_confidence, llm_risk_factor)

    # VIX is gated on level *and* velocity — a sharp intraday spike is
    # risk-off even when the absolute level is still under the ceiling,
    # which is exactly the mid-session regime change a level-only check
    # sleeps through.
    # Direction is recorded in both directions. The gate below only fires on
    # yields RISING, which is the equity-negative case and the one that was
    # threshold-tuned -- but falling yields are a real tailwind for a
    # long-duration index and were previously not visible anywhere in the log.
    yields_direction = (
        "RISING" if tnx.change_bps >= TNX_SPIKE_BPS
        else "FALLING" if tnx.change_bps <= -TNX_SPIKE_BPS
        else "FLAT"
    )
    if oil is not None:
        logger.info(
            "Macro watch — crude %.2f (%+.2f%% from open), 10Y %.3f%% (%+.1fbp, %s), VIX %.2f (%+.2f%%).",
            oil.level, oil.change_pct, tnx.level, tnx.change_bps, yields_direction,
            vix.level, vix.change_pct,
        )

    yields_spiking = tnx.change_bps >= TNX_SPIKE_BPS
    if yields_spiking:
        logger.warning(
            "Yields spiking: 10Y at %.3f%%, %+.1fbp from today's open of %.3f%% — forcing risk-off.",
            tnx.level, tnx.change_bps, tnx.session_open,
        )

    sentiment = "GOOD" if (
        breadth_is_bullish
        and vix.level < VIX_LEVEL_MAX
        and vix.change_pct < VIX_SPIKE_PCT
        and not yields_spiking
        and llm_verdict == "GOOD"
    ) else "BAD"

    # BAD means "unsafe to be LONG" — collapsing breadth, spiking fear,
    # rising yields. Every one of those is a reason a bear put spread should
    # work, so gating short entries on GOOD refused the trade the conditions
    # were actually calling for. Direction gating now lives on the entry side;
    # this flag stays bullish-framed because that is what it measures.
    #
    # A separate halt covers conditions unsafe in EITHER direction. VIX above
    # its ceiling is genuine disorder — wide quotes and gap risk hurt a short
    # spread as much as a long one — as distinct from the velocity terms,
    # which are directional.
    halt = vix.level >= VIX_LEVEL_MAX
    if halt:
        logger.warning(
            "Macro halt: VIX at %.2f is at or above the %.1f ceiling — no entries in either direction.",
            vix.level, VIX_LEVEL_MAX,
        )

    return {
        "market_sentiment": sentiment,
        "macro_halt": halt,
        # 3. Previously generated and discarded every cycle. Logged now so a
        # sentiment flip can be explained after the fact instead of guessed at.
        "macro_confidence": llm_confidence,
        "macro_risk_factor": llm_risk_factor,
        # Watched and recorded, not yet gating — see the fetch above.
        "oil_level": round(oil.level, 2) if oil else 0.0,
        "oil_change_pct": round(oil.change_pct, 2) if oil else 0.0,
        "tnx_level": round(tnx.level, 3),
        "tnx_change_bps": round(tnx.change_bps, 1),
        "yields_direction": yields_direction,
    }


# ---------------------------------------------------------------------------
# Execution risk agent (deterministic rule engine)
# ---------------------------------------------------------------------------


def _is_within_opening_warmup() -> bool:
    """No new entries in the first minutes after the bell.

    The opening auction and the rebalancing that follows it produce whipsaws
    that aren't a trend — the indicators will happily read a direction from
    them, and the engine has no way to tell that reading apart from a real
    one. Waiting for the range to establish costs a few minutes of a session
    the engine mostly sits out anyway.

    Only entries wait. An already-open position is still managed from the
    first cycle, because a stop that ignores the first 15 minutes is worse
    than no stop.
    """
    now_est = datetime.now(ZoneInfo("America/New_York"))
    return (now_est.hour, now_est.minute) < (MARKET_OPEN_HOUR, MARKET_OPEN_MINUTE + WARMUP_MINUTES)


def _is_past_cutoff(cutoff_hour: int = 14) -> bool:
    """No new entries and no BUY_MORE past this hour (EST) — these are
    same-day (0DTE) QQQ spreads, so a fresh or added position needs enough
    of the trading day left to actually work before expiration."""
    now_est = datetime.now(ZoneInfo("America/New_York"))
    return now_est.hour >= cutoff_hour


def is_past_force_close(hour: int = None, minute: int = None) -> bool:
    """Hard close-out cutoff, independent of P&L — QQQ options expire at
    today's close, so any open spread must be flattened before then rather
    than allowed to ride into expiration (assignment/pin risk on the short
    leg, and an OTM long leg simply expires worthless).

    15:30 rather than 15:45. The final half hour is driven by market-on-close
    imbalances, gamma risk peaks and quotes widen, so a 50-cent wiggle can
    erase a large open gain in seconds. That matters much more now that
    trailing exits let winners run instead of booking at a fixed target —
    there is more open profit to protect."""
    hour = FORCE_CLOSE_HOUR if hour is None else hour
    minute = FORCE_CLOSE_MINUTE if minute is None else minute
    now_est = datetime.now(ZoneInfo("America/New_York"))
    return (now_est.hour, now_est.minute) >= (hour, minute)


def execution_risk_agent(state: TradingState, broker: MockBrokerClient = None) -> dict:
    if os.path.exists(KILL_SWITCH_PATH):
        logger.warning("KILL_SWITCH.txt present — halting all algorithmic execution.")
        return {"execution_status": "HALTED", "buy_more_count": state.get("buy_more_count", 0)}

    broker = broker or default_mock_broker()
    sentiment = state.get("market_sentiment")
    halt = bool(state.get("macro_halt"))
    ema9_side = state.get("ema9_side")
    ema_cross = state.get("ema_cross")
    ema50_reject = bool(state.get("ema50_reject"))
    vwap_side = state.get("vwap_side")
    rsi_band = state.get("rsi_band")
    macd = state.get("macd_signal")
    sma = state.get("sma_trend")
    bb = state.get("bollinger_zone")
    bb_cross = state.get("bollinger_cross")
    bb_sd = state.get("bollinger_sd")
    ema9_side = state.get("ema9_side")
    ema_cross = state.get("ema_cross")
    ema50_reject = bool(state.get("ema50_reject"))
    vwap_side = state.get("vwap_side")
    rsi_band = state.get("rsi_band")
    rsi = state.get("rsi_zone")
    count = state.get("buy_more_count", 0)

    position = broker.get_open_position()
    available_cash = broker.get_available_cash()
    past_cutoff = _is_past_cutoff()
    force_close = is_past_force_close()
    in_warmup = _is_within_opening_warmup()

    action = "HOLD"
    exit_reason = ""
    playbook = ""

    if position is not None:
        return_pct = position.return_pct
        # Thresholds belong to the strategy that OPENED the position. A credit
        # spread judged on debit thresholds would read as catastrophically
        # losing the instant it moved at all.
        tp_pct, stop_pct, risk_off_pct = thresholds_for(
            position.playbook, (TAKE_PROFIT_PCT, STOP_LOSS_PCT, RISK_OFF_STOP_LOSS_PCT)
        )
        is_credit_pos = is_credit(position.strategy)
        # Windows that let winners run keep only the stop and the force close.
        # Booking an ITM debit spread at its +30% target gave up half of a
        # structure capped near +64% in total; measured on bullish-stack
        # mornings that cost 18.82 a trade against 36.40, for an identical
        # worst case.
        ride = rides_to_close(position.playbook)

        # Profit ratchet. What matters is whether this position HAS been up,
        # not whether it still is: `gave_back` is only ever evaluated inside
        # the take-profit branch or this one, so a peak that no branch owns is
        # a peak with no protection under it.
        #
        # Armed at the LOWER of the two thresholds, which closes exactly that
        # hole. RATCHET_ARM_PCT is 32 and ITM_GRINDER's target is 30, so a
        # position peaking at +31% used to fall between them: too low to arm
        # the ratchet, and once it slipped back under 30 the take-profit
        # branch stopped running too. It then rode to the -20% stop having
        # been up 31%. Anything that reached its own target is a win worth
        # protecting, whatever the engine-wide arm point says.
        peak_return = max(position.peak_return_pct, return_pct)
        ratchet_armed = peak_return >= min(RATCHET_ARM_PCT, tp_pct)
        giveback = max(
            peak_return * ratchet_giveback_for(position.playbook, TRAIL_GIVEBACK),
            MIN_GIVEBACK_PCT,
        )
        gave_back = peak_return > 0 and return_pct <= peak_return - giveback

        # Rule Z: same-day expiration hard close — overrides P&L entirely.
        if force_close:
            broker.sell_all(position.underlying)
            action, exit_reason = "SELL_ALL", "FORCE_CLOSE"
        # Rule A: Take Profit
        # Judged against the target of the strategy that OPENED this
        # position, not whatever window the clock is in now: an ATM spread is
        # still an ATM spread at 13:00, and holding it to the ITM target would
        # book it early for no reason.
        elif ride:
            # This window rides. Only the stop, the handoff deadline, and the
            # force close can end the trade, so a winner is never truncated by
            # a target or a trail.
            deadline = ride_deadline(position.playbook)
            past_deadline = deadline is not None and datetime.now(NY).time() >= deadline
            # The one number a ride still respects. A debit spread cannot
            # exceed its width, so this is the point where the remaining
            # upside no longer pays for the gamma risk of holding it.
            width = abs(position.short_strike - position.long_strike)
            max_return_pct = (
                (width - position.entry_net_debit) / position.entry_net_debit * 100
                if position.entry_net_debit > 0 else 0.0
            )
            ceiling_pct = RIDE_CEILING_FRACTION * max_return_pct
            if max_return_pct > 0 and return_pct >= ceiling_pct:
                logger.info(
                    "Ride ceiling: %s at %+.1f%% is %.0f%% of the %+.1f%% this structure "
                    "can pay — booking rather than holding for the last few cents.",
                    position.strategy, return_pct, RIDE_CEILING_FRACTION * 100, max_return_pct,
                )
                broker.sell_all(position.underlying)
                action, exit_reason = "SELL_ALL", "TAKE_PROFIT"
            elif return_pct <= stop_pct:
                broker.sell_all(position.underlying)
                action, exit_reason = "SELL_ALL", "STOP_LOSS"
            elif (
                RIDE_RATCHET_ARM_PCT > 0
                and peak_return >= RIDE_RATCHET_ARM_PCT
                and return_pct <= peak_return - max(peak_return * RIDE_GIVEBACK,
                                                    RIDE_MIN_GIVEBACK_PCT)
            ):
                logger.info(
                    "Ride ratchet: peaked at %+.1f%%, now %+.1f%% — booking the ride "
                    "rather than carrying it to the handoff.", peak_return, return_pct,
                )
                broker.sell_all(position.underlying)
                action, exit_reason = "SELL_ALL", "RATCHET"
            elif past_deadline:
                # Hand the single position slot to the next window. Holding a
                # morning winner past 13:25 blocked the credit trade entirely,
                # which measured +36.40 a day against +80.87 for handing over.
                logger.info(
                    "Handoff: closing %s at %+.1f%% so the next window can trade.",
                    position.strategy, return_pct,
                )
                broker.sell_all(position.underlying)
                action, exit_reason = "SELL_ALL", "HANDOFF"
            else:
                action = "TRAILING"
                logger.info(
                    "Riding %s at %+.1f%% to the force close (stop %+.0f%%).",
                    position.strategy, return_pct, stop_pct,
                )
        # Rule A: Take Profit
        elif return_pct >= tp_pct:
            # Target reached. With trailing enabled this arms rather than
            # sells: the position runs while the 5-minute trend holds, so a
            # strong move is not truncated at a number chosen in advance.
            #
            # Two things end the ride: the 9 EMA turning against the
            # structure, and the ratchet below, which measures the position's
            # own giveback. Whichever fires first wins -- a price trail alone
            # knows nothing about P&L, and spread value decays independently
            # of direction.
            #
            # What that does not cover: a violent reversal inside one cycle.
            # Price can round-trip a long way in 60 seconds and the exit only
            # sees it on the next tick. Trailing genuinely trades a capped,
            # certain gain for an uncapped, less certain one.
            # Credit positions trail too, where their window sets a final
            # target. The old rule booked them at the target on the grounds
            # that a credit spread's gain is bounded by the credit collected
            # and so has no tail to run. Bounded is not the same as finished:
            # observed live on 2026-08-20, the 13:30 call credit spread was
            # booked at +51% and was worth +72% forty-five minutes later,
            # with spot four dollars below the short strike. What ends the
            # ride is the same thing that ends a debit ride -- price turning
            # against the structure -- not the target being reached.
            final_tp = final_take_profit_for(position.playbook)
            credit_trails = is_credit_pos and final_tp is not None
            # An adverse move is a move toward the short strike, which is UP
            # for a short call and DOWN for a short put -- the mirror of the
            # debit spreads, and the same 9 EMA reading.
            trend_broken = (
                (position.strategy in (BULL_CALL_SPREAD, PUT_CREDIT_SPREAD)
                 and ema9_side == "BELOW_EMA9")
                or (position.strategy in (BEAR_PUT_SPREAD, CALL_CREDIT_SPREAD)
                    and ema9_side == "ABOVE_EMA9")
            )
            if credit_trails and trend_broken:
                # Direction alone does not threaten short premium -- distance
                # to the short strike does. qqq_close is the same 5-minute
                # series the 9 EMA is built from, so the two agree.
                bar_close = state.get("qqq_close")
                if bar_close:
                    trend_broken = (
                        abs(position.short_strike - float(bar_close)) <= CREDIT_TRAIL_STRIKE_BUFFER
                    )
            hit_final = final_tp is not None and return_pct >= final_tp
            books_at_target = (is_credit_pos and not credit_trails) or not TRAILING_EXITS_ENABLED
            if hit_final or books_at_target or trend_broken or gave_back:
                broker.sell_all(position.underlying)
                action, exit_reason = "SELL_ALL", (
                    "TAKE_PROFIT" if (hit_final or books_at_target)
                    else ("RATCHET" if gave_back else "TRAIL_STOP")
                )
            else:
                action = "TRAILING"
                logger.info(
                    "Trailing %s at %+.1f%% (armed at %+.0f%%, book at %s) — trend intact, letting it run.",
                    position.strategy, return_pct, tp_pct,
                    f"{final_tp:+.0f}%" if final_tp is not None else "trend break",
                )
        # Ratchet rung: armed earlier, has since given back too much. Sits
        # above the stop so a position that was up 45% exits near 38% instead
        # of riding to -10%.
        elif TRAILING_EXITS_ENABLED and ratchet_armed and gave_back:
            logger.info(
                "Profit ratchet: peaked at %+.1f%%, now %+.1f%% (gave back more than %.0f%% of the gain) — closing.",
                peak_return, return_pct, TRAIL_GIVEBACK * 100,
            )
            broker.sell_all(position.underlying)
            action, exit_reason = "SELL_ALL", "RATCHET"
        # Rule B / C: Stop Loss vs. Buy More
        elif return_pct <= stop_pct:
            # place_buy_more adds `position.quantity` more contracts — it
            # doubles the position — so the affordability check has to price
            # that whole lot. Checking a single contract's cost (as this once
            # did) authorised roughly a 5x larger purchase than it verified,
            # and repeated doubling would have compounded the gap: 5 -> 10 ->
            # 20 -> 40 contracts, each step approved by a one-contract test.
            # Never on a credit position. Scaling in doubles the position,
            # and a credit spread's loss is bounded by width-minus-credit
            # rather than by the premium paid -- doubling at the stop roughly
            # doubles an already-maximal loss, on a thesis the market has
            # already disproved. Averaging down into short premium is how
            # accounts die.
            scale_in_cost = position.current_net_value * 100 * position.quantity
            if (
                not is_credit_pos
                and not past_cutoff
                and sentiment == "GOOD"
                and count < MAX_SCALE_INS
                and available_cash >= scale_in_cost
            ):
                broker.place_buy_more(position.underlying, position.quantity)
                action = "BUY_MORE"
            else:
                # Past the 2PM EST cutoff, or sentiment/cash/count don't clear the bar — fall back to stop loss.
                broker.sell_all(position.underlying)
                action, exit_reason = "SELL_ALL", "STOP_LOSS"
        # Rule D: risk-off exit — macro has turned BAD (deteriorating breadth,
        # a VIX spike, or a risk-off headline read) while the position is
        # already losing. Cut at the risk-off level rather than waiting for
        # the full stop: the setup that justified the entry no longer holds,
        # and re-entry is available on any later cycle if conditions recover.
        #
        # Deliberately evaluated *after* the full stop above so a position
        # that already breached the full stop is still recorded as STOP_LOSS
        # — this rule only owns the band between the two thresholds, which
        # keeps the close reasons feeding the setup vector store honest.
        # Only for LONG positions. market_sentiment BAD describes conditions
        # hostile to being long, which are the same conditions a bear put
        # spread profits from — cutting a short early because macro turned
        # bearish would exit the position for the reason it was opened.
        elif (
            sentiment == "BAD"
            and position.strategy == BULL_CALL_SPREAD
            and return_pct <= risk_off_pct
        ):
            logger.warning(
                "Risk-off exit: macro sentiment BAD with position at %.2f%% — closing early "
                # stop_pct, not the module-level STOP_LOSS_PCT: the window's
                # override is what this position is actually judged against,
                # and a log naming the global one sent an entire debugging
                # session chasing a threshold no trade was using.
                "rather than riding to the %.1f%% stop.", return_pct, stop_pct,
            )
            broker.sell_all(position.underlying)
            action, exit_reason = "SELL_ALL", "RISK_OFF"
    # Entry is checked after management rather than as an `elif`, so a cycle
    # that takes profit can immediately look for the next setup instead of
    # sitting flat for a full tick. On a 5-minute cadence that dead cycle was
    # costing a whole bar of a session that only offers ~3 tradeable moves.
    #
    # Only after a TAKE_PROFIT. Re-entering straight after a stop or a
    # risk-off cut would just re-open the setup that had already failed --
    # the signals will still be saying the same thing, so it would churn
    # through the stop repeatedly.
    # RATCHET and TRAIL_STOP belong here too: both can only fire on a
    # position that armed above its target, so they are winning exits under
    # different names. Leaving them out cost the credit window a cycle every
    # time it trailed out instead of booking at the target.
    may_reenter = position is None or exit_reason in ("TAKE_PROFIT", "RATCHET", "TRAIL_STOP")

    # The 14:00 cutoff is a DEBIT rule: a bought spread needs enough day left
    # for the move it is paying for. A credit spread wants the opposite --
    # less time left is less time for the short strike to be reached -- and
    # AFTERNOON_CREDIT is written to run to 15:00 for exactly that reason.
    #
    # Applying the cutoff to it anyway killed the last hour of its window.
    # Observed on 2026-08-20: the window's setup came back at 14:50 and again
    # at 14:51, 14:52 and 14:54, all four inside the window and all four
    # refused by an hour that has nothing to do with short premium. The
    # window's own end time is what bounds a credit entry.
    entry_window = window_for()
    cutoff_blocks_entry = past_cutoff and not (
        entry_window is not None and entry_window.placement == CREDIT
    )

    if broker.get_open_position() is None and may_reenter and not cutoff_blocks_entry and not in_warmup:
        # Bullish: bull call debit spread (long ITM call, short ATM call).
        # Bearish: bear put debit spread (long ITM put, short ATM put). Same
        # market_sentiment=GOOD gate either direction (a calm macro
        # environment is required to open a new position at all). No new
        # entries past the cutoff — a same-day spread opened too late has
        # too little of the trading day left to work before it expires.
        #
        # Both directions now have two distinct setups, mirrored:
        #   - Fade: MACD/EMA one direction while price is stretched to the
        #     OPPOSITE band with RSI at that opposite extreme — a
        #     topping/bottoming reversal read, betting the prior move is
        #     exhausted. (Bullish fade: bearish price action stretched down
        #     to LOWER_BAND/OVERSOLD, momentum turning up. Bearish fade:
        #     bullish price action stretched up to UPPER_BAND/OVERBOUGHT,
        #     momentum turning down.)
        #   - Continuation: MACD/EMA AND price/RSI all agree in the SAME
        #     direction — a breakout/breakdown-in-progress read, betting the
        #     move keeps going. This is what a clean trending move (never
        #     touching the opposite band) looks like, which the fade-only
        #     trigger couldn't catch.
        # Two tiers of the same idea, run side by side so the data decides
        # between them rather than an argument.
        #
        # STRICT is the original gate: MACD, trend, a Bollinger pierce AND an
        # RSI extreme all agreeing. RELAXED drops the RSI requirement only.
        #
        # The reason to try dropping it: a Bollinger pierce and an RSI extreme
        # both measure "price is stretched from its mean", so requiring both
        # is close to asking the same question twice. Measured over a month of
        # 5-minute bars in the entry window, RSI removed 31 of 63 band
        # pierces -- half the setups -- taking the gate from 2.8 to 1.5
        # opportunities a day against a market that only offers about 2.8.
        #
        # But the overlap was 51%, not 90%, so RSI is genuinely filtering
        # rather than duplicating. Whether the half it removes were losers
        # worth avoiding or winners never seen is exactly what is unknown, so
        # both tiers trade and each is attributed separately.
        strict_bull = (
            macd == "BULLISH" and sma == "ABOVE_SMA"
            and ((bb == "LOWER_BAND" and rsi == "OVERSOLD")        # fade
                 or (bb == "UPPER_BAND" and rsi == "OVERBOUGHT"))  # continuation
            and sentiment == "GOOD"
        )
        # Short setups gate on `not halt`, not on GOOD. GOOD means "safe to be
        # long"; requiring it to go short refused every bear put spread in
        # precisely the tape — collapsing breadth, spiking VIX, rising yields —
        # that such a spread exists to profit from.
        strict_bear = (
            macd == "BEARISH" and sma == "BELOW_SMA"
            and ((bb == "UPPER_BAND" and rsi == "OVERBOUGHT")      # fade
                 or (bb == "LOWER_BAND" and rsi == "OVERSOLD"))    # continuation
        )
        relaxed_bull = (
            macd == "BULLISH" and sma == "ABOVE_SMA"
            and bb in ("UPPER_BAND", "LOWER_BAND") and sentiment == "GOOD"
        )
        relaxed_bear = (
            macd == "BEARISH" and sma == "BELOW_SMA"
            and bb in ("UPPER_BAND", "LOWER_BAND")
        )

        # MOMENTUM: price closing back through its 20-period mean with MACD
        # and trend agreeing. A trend-turning thesis rather than a
        # mean-reversion one, so it catches moves that never reach a band.
        #
        # Deliberately NOT "rising RSI", which was the other candidate. RSI
        # merely ticking up fired on 21 bars a day against a market with ~2.8
        # tradeable moves -- it describes the last few bars rather than
        # identifying anything, and trading it would mostly be paying the
        # debit to enter noise.
        # No MACD term. It was vetoing 77% of trend+cross setups over a month
        # of history, cutting this tier from 2.7 opportunities a day to 0.6 --
        # and 2.7 is about what the market actually supplies.
        #
        # A live example of the cost: on a session where QQQ fell $11, MACD
        # read BULLISH on 150 of 219 cycles while price sat BELOW_SMA the
        # whole time. That combination is short-term upticks inside a
        # downtrend, and demanding MACD agree meant every one of the day's 15
        # midline crosses was refused. The engine sat flat through a clean
        # directional move.
        #
        # Trend plus a cross through the 20-period mean is the thesis on its
        # own: price re-crossing its mean in the direction the trend already
        # points. MACD is a second momentum read on the same price series, so
        # requiring it was closer to demanding the same confirmation twice
        # than to adding independent evidence.
        momentum_bull = (
            sma == "ABOVE_SMA" and bb_cross == "CROSS_UP" and sentiment == "GOOD"
        )
        momentum_bear = (
            sma == "BELOW_SMA" and bb_cross == "CROSS_DOWN"
        )

        # CLEAN: the four-rule structural gate. Checked FIRST because it is
        # the most selective -- if it and a looser tier both match, the
        # tighter attribution is the more informative one.
        clean_bull = (
            sma == "ABOVE_SMA" and ema_cross == "EMA9_ABOVE_SMA20"
            and vwap_side == "ABOVE_VWAP" and rsi_band == "BULL_BAND"
            and sentiment == "GOOD"
        )
        clean_bear = (
            sma == "BELOW_SMA" and ema_cross == "EMA9_BELOW_SMA20"
            and vwap_side == "BELOW_VWAP" and rsi_band == "BEAR_BAND"
        )

        # REJECT: price tried the 50 EMA and failed. Requires the broader
        # trend to already be down, so this is a continuation read rather than
        # a lone candle pattern.
        reject_bear = (
            ema50_reject and sma == "BELOW_SMA" and vwap_side == "BELOW_VWAP"
        )

        # TREND: momentum, direction and an RSI extreme agreeing, no band
        # needed. This is the tier that can trade a sustained move, which is
        # precisely what the band-based tiers cannot see.
        trend_bull = (
            macd == "BULLISH" and sma == "ABOVE_SMA"
            and rsi == "OVERBOUGHT" and sentiment == "GOOD"
        )
        trend_bear = (
            macd == "BEARISH" and sma == "BELOW_SMA" and rsi == "OVERSOLD"
        )

        # A single guard rather than the same clause repeated on six
        # conditions: VIX at or above its ceiling is disorder, and disorder is
        # not directional — wide quotes and gap risk hurt a short spread as
        # much as a long one. Everything below assumes it has already passed.
        # Refuse the side that just lost, for the cooldown period. The
        # signals still read the same after a stop-out, so without this the
        # engine immediately re-enters the trade the market just rejected.
        cooling = blocked_direction()
        if cooling == "bearish":
            strict_bear = relaxed_bear = momentum_bear = trend_bear = clean_bear = False
        elif cooling == "bullish":
            strict_bull = relaxed_bull = momentum_bull = trend_bull = clean_bull = False

        # Short setups are suppressed until the bearish start time, whatever
        # the signals say. Long setups are unaffected.
        if _is_before_bearish_start():
            strict_bear = relaxed_bear = momentum_bear = trend_bear = clean_bear = False

        if halt:
            tier, bullish = None, False
        elif CLEAN_ENTRIES_ENABLED and (clean_bull or clean_bear):
            tier, bullish = "CLEAN", clean_bull
        elif strict_bull or strict_bear:
            tier, bullish = "STRICT", strict_bull
        elif RELAXED_ENTRIES_ENABLED and (relaxed_bull or relaxed_bear):
            tier, bullish = "RELAXED", relaxed_bull
        elif MOMENTUM_ENTRIES_ENABLED and (momentum_bull or momentum_bear):
            tier, bullish = "MOMENTUM", momentum_bull
        elif REJECT_ENTRIES_ENABLED and reject_bear:
            tier, bullish = "REJECT", False
        elif TREND_ENTRIES_ENABLED and (trend_bull or trend_bear):
            tier, bullish = "TREND", trend_bull
        else:
            tier, bullish = None, False

        # A session already this far down is not a place to be long, whatever
        # the five-minute averages say about the last twenty minutes.
        if (
            tier is not None and bullish and DAY_TREND_MAX_DROP_PCT > 0
            and state.get("session_move_pct") is not None
            and float(state.get("session_move_pct") or 0.0) <= -DAY_TREND_MAX_DROP_PCT
        ):
            logger.info(
                "Session is %.2f%% off the open — refusing the bullish %s entry.",
                float(state.get("session_move_pct") or 0.0), tier,
            )
            tier, bullish = None, False

        # No tier, but a credit window is open: sell premium on the side the
        # trend is moving away from. Recorded as its own tier name so the
        # scoreboard can separate it from the signal-driven entries.
        if tier is None and CREDIT_LOOSE_GATE and not halt:
            w = entry_window
            if w is not None and w.placement == CREDIT and sma in ("ABOVE_SMA", "BELOW_SMA"):
                tier, bullish = "THETA", sma == "ABOVE_SMA"

        if tier is not None:
            # Strike placement comes from the time-of-day window, so the same
            # signal produces a leveraged ATM structure during the morning
            # momentum leg and a positive-theta ITM one through the midday
            # lull. window is None outside every window — that is a no-entry
            # period, including any gap left by retiring a strategy.
            # Resolved once, above the entry gate, because the cutoff check
            # needs to know whether this is a credit window.
            window = entry_window

            # A window may restrict which tiers can open it. MORNING_DRIFT
            # takes CLEAN only: the same ITM structure measured +18.74 a
            # trade when the full bullish stack held and -4.49 when it did
            # not, and the looser tiers are precisely what would open it on
            # the days that lose.
            if window is not None and not window.allows_tier(tier):
                logger.info(
                    "%s does not accept the %s tier — no entry.", window.name, tier,
                )
                window = None
                action = "TIER_NOT_ALLOWED"

            # Direction gate. The tier ladder is symmetric but the measured
            # edge is not: on bearish-stack mornings the bear put spread
            # returned -15.52 a trade at a 25% win rate, and its sign was not
            # even stable across a five-minute shift of the judging bar.
            if window is not None and not window.allows_direction(bullish):
                logger.info(
                    "%s is long-only — refusing the bearish %s setup.", window.name, tier,
                )
                window = None
                action = "DIRECTION_NOT_ALLOWED"

            # A window may also size itself, because one fraction cannot serve
            # both structures — see PlaybookWindow.entry_fraction.
            entry_fraction = ENTRY_FRACTION
            if window is not None and window.entry_fraction is not None:
                entry_fraction = window.entry_fraction

            # Size against realized equity rather than the static budget:
            # after a run of losses the account is smaller and the position
            # should be too. And stop opening anything once the day's losses
            # reach the limit. An already-open position is still managed,
            # since refusing to manage what you hold is not risk control.
            #
            # Both checks run before the price fetch below, so a halted or
            # out-of-window cycle costs nothing.
            eq = current_equity(POSITION_BUDGET)
            # Hoisted: sizing below needs to know whether this entry follows
            # a loss, and a second query would just ask the same question of
            # the same rows.
            streak = 0
            if eq.halted:
                window = None
                action = "HALTED_DAILY_LOSS"
            else:
                # A run of losses says the strategy does not fit today's tape,
                # and that can be true well before the dollar limit is hit.
                # window is None here whenever an earlier gate refused the
                # setup, so the exemption has to be read defensively.
                exempt = window is not None and window.exempt_from_streak_halt
                streak = consecutive_losses_today()

                if streak >= MAX_CONSECUTIVE_LOSSES and not exempt:
                    logger.warning(
                        "%d consecutive losing trades today — standing down for the session.", streak,
                    )
                    window = None
                    action = "HALTED_LOSS_STREAK"
                elif streak >= MAX_CONSECUTIVE_LOSSES:
                    # The dollar cap above still governs this window, so risk
                    # stays bounded. Three morning stops cost $111 against a
                    # $200 cap yet would otherwise forfeit the credit trade,
                    # which is the larger edge by roughly three to one.
                    logger.info(
                        "%d consecutive losses, but %s is exempt from the streak halt — "
                        "the daily cap still applies.", streak, window.name,
                    )

            if window is not None:
                spot = fetch_qqq_spot()
                atm_strike = round_to_strike(spot)
                is_credit_window = window.placement == CREDIT
                # One chain for placement and pricing; it is cached for the
                # cycle, and every use of it falls back to the model.
                try:
                    chain = fetch_option_chain()
                except Exception:
                    logger.exception("Chain fetch failed — pricing from the model.")
                    chain = {}

                if is_credit_window:
                    # Bullish sells puts below spot, bearish sells calls above.
                    strategy = PUT_CREDIT_SPREAD if bullish else CALL_CREDIT_SPREAD
                    short_strike, long_strike = credit_strikes_for(window, atm_strike, bullish, bb_sd)
                    delta_strike = strike_for_delta(
                        chain, option_type_for(strategy), CREDIT_SHORT_DELTA) if chain else None
                    if delta_strike is not None:
                        short_strike = delta_strike
                        long_strike = (short_strike + window.width if not bullish
                                       else short_strike - window.width)
                        logger.info(
                            "Credit strikes by delta: short %.0f (~%.2f delta), long %.0f — "
                            "sigma placement would have used %.0f.",
                            short_strike, CREDIT_SHORT_DELTA, long_strike,
                            credit_strikes_for(window, atm_strike, bullish, bb_sd)[0],
                        )
                else:
                    strategy = BULL_CALL_SPREAD if bullish else BEAR_PUT_SPREAD
                    placement = window
                    if (
                        TRENDING_LONG_DEPTH > 0
                        and float(state.get("adx") or 0.0) >= TRENDING_ADX_MIN
                    ):
                        placement = _dc_replace(window, long_depth=TRENDING_LONG_DEPTH)
                        logger.info(
                            "ADX %.1f — placing the long leg $%.0f ITM instead of $%.0f, "
                            "so the short strike leaves room above.",
                            float(state.get("adx") or 0.0), TRENDING_LONG_DEPTH,
                            window.long_depth if window.long_depth is not None else window.width,
                        )
                    long_strike, short_strike = strikes_for(placement, atm_strike, bullish)

                # Price the entry with the same model that reprices it next
                # cycle. Sizing uses that price too, or the position costs
                # something other than the budget it was sized against.
                if is_credit_window:
                    # Selling: you receive the BID, and size against capital
                    # at risk (width - credit) rather than the credit itself.
                    # A $3-wide sold for $0.40 collects $40 a contract and can
                    # lose $260 -- sizing on the credit understates exposure
                    # more than sixfold.
                    model_mid = estimate_credit_value(strategy, short_strike, long_strike, spot)
                    net_debit = fill_price(model_mid, "sell")
                    # What the market would actually pay for it. Selling a
                    # vertical fills at its natural bid: the short leg's bid
                    # against the long leg's ask.
                    market = chain_vertical(chain, option_type_for(strategy),
                                            short_strike, long_strike) if chain else None
                    if market is not None:
                        net_debit = max(market["bid"], 0.0)
                    quantity = broker.estimate_credit_quantity(
                        eq.equity * entry_fraction, net_debit, window.width
                    )
                else:
                    # Buying: you pay the ask.
                    model_mid = estimate_spread_value(strategy, long_strike, short_strike, spot)
                    net_debit = fill_price(model_mid, "buy")
                    # Buying a vertical fills at its natural ask.
                    market = chain_vertical(chain, option_type_for(strategy),
                                            long_strike, short_strike) if chain else None
                    if market is not None and market["ask"] > 0:
                        net_debit = market["ask"]
                    quantity = broker.estimate_spread_quantity(eq.equity * entry_fraction, net_debit)

                # Half size after a loss. The cooldown decides WHETHER to
                # take the next trade; this decides how big it is, and a
                # trade taken into a tape that has just stopped one out is
                # the wrong place to be larger than usual.
                if streak > 0 and quantity > 1:
                    logger.info(
                        "Re-entry after %d loss(es) today — halving size from %d to %d contracts.",
                        streak, quantity, quantity // 2,
                    )
                    quantity = quantity // 2

                # Risk allocation. The entry fraction has already said how
                # much capital to deploy; this says how much of the day's
                # loss budget that trade is allowed to consume, which is the
                # number the daily cap is actually written in.
                risk_share = (
                    REENTRY_RISK_SHARE if streak > 0
                    else risk_share_for(window.name, DEFAULT_RISK_SHARE)
                )
                stop_pct_for_entry = thresholds_for(
                    window.name, (TAKE_PROFIT_PCT, STOP_LOSS_PCT, RISK_OFF_STOP_LOSS_PCT)
                )[1]
                # net_debit is the premium paid on a debit spread and the
                # credit received on a credit one, and the stop is a
                # percentage of exactly that number in both cases -- so one
                # expression prices the intended loss for either structure.
                risk_per_contract = abs(stop_pct_for_entry) / 100.0 * net_debit * 100
                risk_budget = risk_share * eq.daily_loss_limit
                if risk_per_contract > 0:
                    max_by_risk = int(risk_budget // risk_per_contract)
                    if max_by_risk < quantity:
                        logger.info(
                            "Risk allocation: %s may spend %.0f%% of the $%.0f daily budget "
                            "($%.0f); one contract stops at $%.0f — sizing %d contracts, not %d.",
                            window.name, risk_share * 100, eq.daily_loss_limit,
                            risk_budget, risk_per_contract, max_by_risk, quantity,
                        )
                        quantity = max_by_risk

                # The tail cap. Structural loss is the premium paid on a debit
                # spread and width-minus-credit on a credit one -- what the
                # position loses when the stop does not get a chance to work.
                structural_per_contract = (
                    (window.width - net_debit) * 100 if is_credit_window else net_debit * 100
                )
                if quantity > 0 and structural_per_contract > 0:
                    max_by_tail = int((MAX_POSITION_RISK_PCT * eq.equity) // structural_per_contract)
                    if max_by_tail < quantity:
                        logger.info(
                            "Tail cap: %d contracts would put $%.0f at structural risk, above "
                            "%.0f%% of $%.0f equity — sizing %d.",
                            quantity, quantity * structural_per_contract,
                            MAX_POSITION_RISK_PCT * 100, eq.equity, max_by_tail,
                        )
                        quantity = max_by_tail

                if is_credit_window and 0 < quantity and net_debit < MIN_CREDIT:
                    logger.info(
                        "%s priced at %.3f credit, below the %.2f floor — no entry. "
                        "A spread sold for nothing carries the full width of risk.",
                        window.name, net_debit, MIN_CREDIT,
                    )
                    quantity = 0

                if quantity > 0:
                    # What the market says this spread is worth, next to what
                    # the model just decided it is worth. Logged at the moment
                    # of entry because that is where the two can be compared
                    # against a price that is about to be committed to.
                    #
                    # A credit spread's model value is its cost to CLOSE, so
                    # the short strike is the long leg of that vertical --
                    # the same argument order estimate_credit_value uses.
                    try:
                        buy_leg, sell_leg = (
                            (short_strike, long_strike) if is_credit_window
                            else (long_strike, short_strike)
                        )
                        log_price_divergence(
                            option_type_for(strategy), buy_leg, sell_leg, model_mid,
                            f"entry {window.name}",
                        )
                    except Exception:
                        # Never let an observational log stop an entry.
                        logger.exception("Chain divergence log failed — entering anyway.")

                    playbook = f"{window.name}:{tier}"
                    logger.info(
                        "Entering %s via %s: %s %d contracts, long %.1f / short %.1f at $%.2f "
                        "(equity $%.2f, target +%.0f%%)",
                        "BULL" if bullish else "BEAR", playbook, window.placement,
                        quantity, long_strike, short_strike, net_debit,
                        eq.equity, window.take_profit_pct,
                    )
                    if is_credit_window:
                        broker.place_credit_spread(strategy, "QQQ", quantity,
                                                   short_strike, long_strike, net_debit, playbook)
                        action = "SELL_PUT_CREDIT" if bullish else "SELL_CALL_CREDIT"
                    elif bullish:
                        broker.place_bull_call_spread("QQQ", quantity, long_strike, short_strike, net_debit, playbook)
                        action = "BUY_CALL_SPREAD"
                    else:
                        broker.place_bear_put_spread("QQQ", quantity, long_strike, short_strike, net_debit, playbook)
                        action = "BUY_PUT_SPREAD"

    return {
        "execution_status": action,
        "exit_reason": exit_reason,
        "playbook": playbook,
        "buy_more_count": count + 1 if action == "BUY_MORE" else count,
    }
