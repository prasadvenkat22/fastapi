"""Replay past sessions through the real engine, sweeping one parameter.

The comments in playbook.py cite sweeps -- widths, entry times, stop
distances -- that no committed tool could reproduce. This is that tool.

It does NOT reimplement the strategy. It swaps out the clock, the data feed
and the database, then runs the engine's own indicator agents and its own
execution_risk_agent bar by bar, so a result here is what the deployed code
would have done rather than what a second implementation of it thinks.

    python scripts/sweep.py morning     # widths x long-leg depth
    python scripts/sweep.py credit      # credit window widths
    python scripts/sweep.py condor      # the structure we log but never trade
    python scripts/sweep.py events      # what scheduled macro days actually cost
    python scripts/sweep.py forceclose # how late should the day be flattened?
    python scripts/sweep.py mstop      # is the morning stop too tight for the new structure?
    python scripts/sweep.py worstdays  # anatomy of the losing sessions
    python scripts/sweep.py taper      # tighten the ratchet as a position nears its max
    python scripts/sweep.py severity   # trade only SEVERE bad mornings, not any bearish read
    python scripts/sweep.py badmorning # does a bearish credit window fill the gap?
    python scripts/sweep.py latestop   # tighter credit stop into peak gamma?
    python scripts/sweep.py hours       # when in the day does QQQ actually move?
    python scripts/sweep.py weekday     # is Monday or Friday a different market?
    python scripts/sweep.py macro       # do yields, crude and VIX predict our day?
    python scripts/sweep.py placement   # morning width x depth, judged per DAY
    python scripts/sweep.py condorvs    # both sides vs the one side we already sell
    python scripts/sweep.py trenddepth  # uncap the spread when the trend is strong?
    python scripts/sweep.py daytrend    # refuse longs once the session is down
    python scripts/sweep.py daytype     # what the engine earns by market regime
    python scripts/sweep.py squeeze     # does a volatility squeeze make a morning safe to sell?
    python scripts/sweep.py bearside    # may the morning trade the short side too?
    python scripts/sweep.py ratchetstyle # dollar-offset ratchet vs share-of-peak
    python scripts/sweep.py cooldown    # how long to stand down after a loss
    python scripts/sweep.py size        # fewer, larger trades vs more, smaller ones
    python scripts/sweep.py daily       # does trading MORE per day earn more per day?
    python scripts/sweep.py creditexit  # giveback and window length for the credit trade
    python scripts/sweep.py creditgate  # does the credit window need a directional tier?
    python scripts/sweep.py rideratchet # profit protection for a riding position
    python scripts/sweep.py rideguard   # ratchet vs absolute take-profit on a ride
    python scripts/sweep.py ridetight   # a ratchet tight enough to act like a level
    python scripts/sweep.py retries     # morning window length: does a second entry pay?
    python scripts/sweep.py mstart      # may the morning open at 09:45 instead of 10:15?
    python scripts/sweep.py handoff     # should the morning still hand its slot over at 13:25?
    python scripts/sweep.py daydrop     # does the morning long want the day to have fallen first?
    python scripts/sweep.py breach      # model-free: how often a strike distance holds
    python scripts/sweep.py windows     # every window solo: which earns, which does not
    python scripts/sweep.py orb         # single-leg weekly calls on an opening-range break

What it cannot replay, and what that costs:

  * Macro sentiment and the VIX halt came from an LLM and a live feed. Every
    session here is assumed GOOD and un-halted, so bullish entries the macro
    gate would have refused are included. That flatters every long result
    equally, which is fine for RANKING variants against each other and wrong
    for predicting what a window will earn.
  * Premiums come from broker.py's model, not from a chain. The chain-vs-
    model divergence logging added on 2026-08-20 exists to put a number on
    that gap; until it has, treat absolute dollars as indicative and
    differences between variants as the real output.
  * yfinance serves 60 days of 5-minute bars, so the sample is what it is.
"""

import math
import os
import sys
from dataclasses import replace
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

import trading_engine.broker as broker_mod
import trading_engine.nodes as N
import trading_engine.playbook as PB
from trading_engine.broker import (CALL_CREDIT_SPREAD, PUT_CREDIT_SPREAD, MockBrokerClient,
                                   estimate_credit_value, estimate_spread_value, fill_price,
                                   is_credit, round_to_strike)
from trading_engine.equity import EquityState

NY = ZoneInfo("America/New_York")
# Starting equity for every arm. Read from the same variable the engine
# uses, not hardcoded: it was 10000.0 while the deployed budget was 4000,
# which is the other half of the sizing defect the banner above exists to
# prevent. Sizing is a share of equity, so a harness with 2.5x the
# account's equity reports 2.5x the account's dollars.
EQUITY = float(os.getenv("TRADING_POSITION_BUDGET", "10000"))

# Round-trip cost of crossing the spread, in premium terms, per contract.
#
# Measured on real QQQ quotes on 2026-08-21: a two-leg vertical's natural
# bid/ask gap was 0.15 on the morning debit spread (3.88/4.03) and 0.02 on
# the afternoon credit spread (0.59/0.61) -- call it 0.10 a contract as a
# round trip, or $10.
#
# Charging it matters for more than realism. Every variant that trades more
# often pays it more often, and a sweep that ignores it systematically
# flatters the busiest configuration -- which is precisely the axis several
# of these sweeps are deciding.
SLIPPAGE_ROUNDTRIP = 0.10

# Options trade on a PRICE GRID, and this harness has never modelled it.
#
# broker.fill_price rounds to four decimals, so a replay can mark a spread at
# 0.0437 and book a gain of a third of a cent. No such fill exists. QQQ
# options quote in pennies below $3.00 and nickels above, and every real exit
# lands on that grid.
#
# It does not matter much at $3-4 -- a penny on the morning debit spread is
# 0.3% -- and it decides everything at a nickel. Live on 2026-08-27 the
# credit window sold 721/723 for 0.05, ratcheted out at a measured +20%, and
# booked exactly $0.00: the peak-to-exit move the ratchet was reading was
# ONE TICK, and crossing the spread to act on it consumed the whole of it.
# A smooth-price harness cannot produce that result, which is why no previous
# sweep of this window ever showed it.
#
# Buys round UP to the next tick and sells round DOWN, which is the direction
# a crossing order actually fills.
def _flag(name: str, default: bool) -> bool:
    """One truthiness convention for every SWEEP_ flag.

    These disagreed. SWEEP_TICK_PRICING tested == "true", SWEEP_CHAIN_PRICING
    tested == "1", and each silently read the other's value as OFF. Both have
    now cost a run: the tick grid once (recorded above), and chain pricing on
    2026-09-03, when a late-stall sweep exported SWEEP_CHAIN_PRICING=true and
    spent twenty minutes measuring MODEL prices while its own banner said so.

    The banner is what caught it both times, which is the argument for the
    banner -- but a flag that reads its own value as false is not a thing to
    keep catching.
    """
    return (os.getenv(name, "1" if default else "0").strip().lower()
            in ("1", "true", "yes", "on"))


TICK_PRICING = _flag("SWEEP_TICK_PRICING", True)
TICK_BELOW_3 = 0.01
TICK_ABOVE_3 = 0.05
TICK_BREAK = 3.0


def _to_tick(price: float, side: str) -> float:
    """Snap a fill to the grid the contract actually trades on."""
    if price <= 0:
        return 0.0
    tick = TICK_BELOW_3 if price < TICK_BREAK else TICK_ABOVE_3
    n = price / tick
    # The epsilon keeps a price already ON the grid from being nudged a whole
    # tick by floating-point dust -- 0.07/0.01 is 6.999999999999999.
    n = math.ceil(n - 1e-9) if side == "buy" else math.floor(n + 1e-9)
    return max(round(n * tick, 4), 0.0)


def _patch_ticks():
    """Quantise every simulated fill, in every namespace that took a copy."""
    if not TICK_PRICING:
        return
    raw = broker_mod.fill_price

    def fill_price_ticked(model_value: float, side: str) -> float:
        return _to_tick(raw(model_value, side), side)

    for mod in (broker_mod, N, sys.modules[__name__]):
        if hasattr(mod, "fill_price"):
            mod.fill_price = fill_price_ticked

# Commission, in premium terms, per contract per ROUND TRIP.
#
# Tradier charges $0.35 a contract per LEG, confirmed against the account by
# scaling a preview order: 1 contract of a vertical is 2 legs and $0.70 to
# open, so a round trip is 4 legs and $1.40 -- 0.014 in the premium units this
# harness works in.
#
# It falls very unevenly. Against the credit window's +32.50 a trade at four
# contracts it is $5.60, about 17% of the edge; against the morning window's
# far larger per-trade figure it is negligible. That is the same asymmetry the
# bid-ask shows, and the same reason a $1-wide credit spread measured -83.85 a
# trade: fixed per-contract costs hurt small premiums disproportionately.
COMMISSION_ROUNDTRIP = 0.014


# Stand the session down after a RIDE ratchets out, instead of freeing the
# slot. Lives HERE and not in nodes.py: the engine's may_reenter is a
# per-cycle decision and a ride's re-entry happens on a LATER cycle, where
# `position is None` short-circuits it -- a knob added there measured
# byte-identical results and was removed. Standing a window down across
# cycles is session state, and for a diagnostic the harness is the right
# place for it.
STAND_DOWN_AFTER_RIDE_RATCHET = False

# MODEL A STOP THAT ACTUALLY FIRES AT ITS SETTING.
#
# The harness evaluates exits once per BAR. On 5-minute bars that is a stop
# checked every five minutes, and the deployed engine now checks every ten
# seconds. The gap is not cosmetic: measured over 60 sessions the 19 losing
# morning trades realised a MEAN of -28.3% against a stop set to -8%, and
# the worst realised -66.4% inside a single bar. Every counter-trend idea
# tested here -- a 09:45 start, the bullish band bounce, the stall rule --
# fails through exactly that overshoot, so the harness has been penalising
# them for a defect the live engine no longer has.
#
# 1-minute bars cannot fix it: yfinance serves seven days of them and this
# sample is sixty sessions. But the bars carry HIGH and LOW, so the
# question 'would the stop level have been touched inside this bar' is
# answerable without finer data.
#
# On, the position is priced at the bar's ADVERSE extreme -- the low for a
# long, the high for a short -- and if that breaches the stop the trade is
# closed AT THE STOP LEVEL rather than at the bar close. That is the
# optimistic bound: a perfect stop. Off is the pessimistic bound: a stop
# that waits five minutes. The live engine sits between them, nearer the
# optimistic end, and reporting both is more honest than picking one.
# DEFAULT FALSE, AND IT SHOULD USUALLY STAY THERE.
#
# This prices the spread at the BAR'S LOW and books the stop there, on the
# argument that a stop firing mid-bar fires before the bar close decides
# anything. That argument holds for a resting stop ORDER. The engine does not
# place one: it reads the mark once a minute from the live quote and sells at
# market, so it cannot see a thirty-second dip inside a five-minute bar.
#
# MEASURED AGAINST A REAL TRADE. QQQ 2026-09-03, MORNING_DRIFT, live bought a
# 710/720 at 2.83 and sold at 5.22 for +478:
#
#     intrabar ON    entry 2.74 -> "exit 3.75"  STOP_LOSS  -159.80
#     intrabar OFF   entry 2.74 ->  exit 5.20   STALL      +469.20
#
# With it on, the harness stops out at 10:55 a position the engine rode to
# 11:05. Note the first row is also self-inconsistent: exit 3.75 against a
# 2.74 entry is a GAIN, because the pnl is recomputed at the stop level while
# exit_value keeps the bar-close mark. The number is right for a stop that
# could fire; the stop could not fire.
#
# Turn it on only to ask what a resting stop order WOULD have done, and never
# to tune a parameter the live engine acts on.
INTRABAR_STOPS = _flag("SWEEP_INTRABAR_STOPS", False)

# STALLED-PEAK EXIT. Book when the profit curve stops making new highs.
#
# Different from a ratchet, which fires on giveback alone and therefore
# cannot tell a dip inside a climb from the end of the climb. This asks a
# second question: has the position made a NEW peak recently? While it
# keeps setting higher highs the rule waits, however far it has pulled
# back; once it has gone STALL_MINUTES without a new high AND is below the
# peak by STALL_GIVEBACK_PCT, it books.
#
# The motivating shape is 2026-08-28: peaked +59.0% at 11:26, never made
# another high, then collapsed. A ratchet fires on the first dip; a stall
# detector waits through dips and acts on the absence of progress.
#
# No longer harness-only. The peak's TIMESTAMP now exists on the position
# row (migration d1f7a03c9e84 added peak_at) and the rule is DEPLOYED at
# TRADING_STALL_MINUTES=5 / TRADING_STALL_GIVEBACK_PCT=5.
#
# Read from those same two variables, so a sweep invoked with the live
# environment models the live exit ladder instead of a version of it with
# the stall switched off. Defaulting to 0 keeps every earlier reproduction
# in strategy_notes.txt reproducible: a run without the variables behaves
# exactly as it did when those sections were written.
#
# This was a silent divergence of the section-36 kind -- the harness modelled
# a different engine from the deployed one and nothing in the output said so.
# It is in the banner now.
# A DIFFERENT QUIET TIMER FOR THE LAST STRETCH OF THE DAY.
#
# The claim under test: after roughly 15:15 the tape gets noisy as people
# close, so a 5-minute quiet timer books positions on chop that would have
# recovered by the bell. Raised by a live MU 930/950 stalled out at 15:17 for
# -135 that finished 845 higher.
#
# One example is not evidence -- section 27 and section 51 both looked like
# this and did not survive -- so the knob exists to MEASURE it. None means the
# timer never changes, which is what is deployed.
LATE_STALL_AFTER = None       # dtime, or None to leave the timer alone
LATE_STALL_MINUTES = 0.0      # 0 disables the stall entirely after the cutoff
STALL_MINUTES = float(os.getenv("TRADING_STALL_MINUTES", "0"))
STALL_GIVEBACK_PCT = float(os.getenv("TRADING_STALL_GIVEBACK_PCT", "0"))


class _Clock:
    """The simulated 'now', shared by every patched time function."""
    now = datetime(2026, 1, 1, 9, 45, tzinfo=NY)


class _FakeDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return _Clock.now


class _Cooldown:
    """The post-loss cooldown and the loss streak, simulated.

    Both were stubbed to "never blocking" in the first version of this
    harness, which quietly made two of the engine's risk rules invisible to
    every sweep run through it -- including any sweep of the cooldown itself.
    """
    minutes = 30.0
    win_minutes = 0.0
    last_at = None
    last_pnl = None
    last_dir = None
    streak_today = 0

    BULLISH = ("BULL_CALL_SPREAD", "PUT_CREDIT_SPREAD")

    @classmethod
    def open_session(cls):
        cls.last_at = None
        cls.last_pnl = None
        cls.last_dir = None
        cls.streak_today = 0

    @classmethod
    def book(cls, strategy: str, pnl: float, ts):
        # The LAST trade, win or lose -- not the last LOSS.
        #
        # This used to remember only losses, so a loss at 10:30 still blocked
        # its direction at 10:50 even after a win at 10:40 had intervened.
        # equity.blocked_direction reads a single row ordered by closed_at and
        # returns None when that row is a win, so the harness was modelling a
        # stickier cooldown than the deployed one. Same class of divergence as
        # the stall exit and the sizing default: it ranked arms consistently
        # and described an engine that does not exist.
        cls.last_at, cls.last_pnl = ts, pnl
        cls.last_dir = "bullish" if strategy in cls.BULLISH else "bearish"
        if pnl < 0:
            cls.streak_today += 1
        else:
            cls.streak_today = 0

    @classmethod
    def blocked(cls):
        if cls.last_at is None or cls.minutes <= 0 or (cls.last_pnl or 0) >= 0:
            return None
        age = (_Clock.now - cls.last_at).total_seconds() / 60.0
        return cls.last_dir if age < cls.minutes else None

    @classmethod
    def win_blocked(cls):
        """Both directions, after a WINNING exit. equity.win_pause_active."""
        if cls.last_at is None or cls.win_minutes <= 0 or (cls.last_pnl or 0) <= 0:
            return False
        return (_Clock.now - cls.last_at).total_seconds() / 60.0 < cls.win_minutes

    @classmethod
    def streak(cls):
        return cls.streak_today


class _Account:
    """Equity that actually moves, so sizing and the circuit breaker are real.

    The first version of this harness handed the engine a fixed $10,000 with
    halted=False on every cycle of every session. That silently removed two
    things the live engine has: the daily loss cap, which stops entries once
    the session is down its limit, and compounding, which makes every
    position size a function of what the account has already made. Neither
    matters while sizing is held constant -- and both are the entire question
    the moment sizing is what is being swept.
    """
    equity = EQUITY
    realized_today = 0.0
    cap_pct = 0.06

    @classmethod
    def open_session(cls):
        cls.realized_today = 0.0

    @classmethod
    def book(cls, pnl: float):
        cls.equity += pnl
        cls.realized_today += pnl

    @classmethod
    def state(cls) -> EquityState:
        session_start = cls.equity - cls.realized_today
        limit = max(session_start, 0.0) * cls.cap_pct
        return EquityState(EQUITY, cls.equity - EQUITY, cls.equity, cls.realized_today,
                           limit, limit > 0 and cls.realized_today <= -limit)


# Price with the chain-calibrated Black-Scholes pricer instead of broker.py's
# probability approximation. Off by default so that every result already
# committed in playbook.py can still be reproduced by re-running this file;
# turn it on with SWEEP_CHAIN_PRICING=1 and the two are one flag apart.
CHAIN_PRICING = _flag("SWEEP_CHAIN_PRICING", False)

# Session VIX, so the smile scales to the day being replayed rather than
# pricing a March panic at the quiet session it was fitted on.
_VIX_BY_DAY: dict = {}


def _session_vix() -> "float | None":
    return _VIX_BY_DAY.get(_Clock.now.date())


def _patch_pricing():
    """Swap broker.py's vertical pricing for the chain-calibrated one.

    Both call sites are patched in every namespace that reached for them --
    broker itself, nodes, and this file's own imported names. Missing one
    leaves half the replay on the old pricer, which would show up as a
    result that moves when the flag is toggled but by less than it should.
    """
    import trading_engine.chain_pricer as CP

    def spread_value(strategy, long_strike, short_strike, spot, minutes_left=None):
        mins = broker_mod.minutes_to_expiry() if minutes_left is None else minutes_left
        return round(CP.vertical_value(spot, mins, long_strike, short_strike,
                                       strategy == broker_mod.BULL_CALL_SPREAD,
                                       _session_vix()), 4)

    def credit_value(strategy, short_strike, long_strike, spot, minutes_left=None):
        mins = broker_mod.minutes_to_expiry() if minutes_left is None else minutes_left
        return round(CP.credit_value(spot, mins, short_strike, long_strike,
                                     strategy == broker_mod.CALL_CREDIT_SPREAD,
                                     _session_vix()), 4)

    for mod in (broker_mod, N, sys.modules[__name__]):
        if hasattr(mod, "estimate_spread_value"):
            mod.estimate_spread_value = spread_value
        if hasattr(mod, "estimate_credit_value"):
            mod.estimate_credit_value = credit_value


def _patch_engine():
    """Point the engine at simulated time and account state."""
    N.datetime = _FakeDatetime
    PB.datetime = _FakeDatetime
    broker_mod.datetime = _FakeDatetime
    # macro_calendar asks the clock what day it is, so without this the
    # blackout is evaluated against the real date and never fires in a
    # replay -- three identical rows, the same tell as the two sweeps before
    # it that were silently testing nothing.
    import trading_engine.macro_calendar as CAL
    CAL.datetime = _FakeDatetime
    N.blocked_direction = _Cooldown.blocked
    N.consecutive_losses_today = _Cooldown.streak
    N.win_pause_active = _Cooldown.win_blocked
    _Cooldown.win_minutes = float(os.getenv("TRADING_WIN_COOLDOWN_MINUTES", "0"))
    N.current_equity = lambda *_: _Account.state()
    # Observational only, and it would fire a live HTTP request per bar.
    N.log_price_divergence = lambda *a, **k: None
    # No chain, deliberately. Entries price from the chain since 2026-08-21,
    # but there are no HISTORICAL chains to replay -- and reaching for today's
    # would price a June entry at August's quotes. Left in, the first run of
    # this sweep filled debit spreads at an expired chain's penny asks and
    # reported +43,559 a trade.
    #
    # That used to end "the model is the only pricing a replay can use, which
    # is precisely why a premium-selling window cannot be validated by one."
    # SWEEP_CHAIN_PRICING=1 is the way out of that: not a historical chain,
    # but a pricer CALIBRATED to one. See trading_engine/chain_pricer.py --
    # against 1,944 logged marks it cuts the error 82%, and on the two
    # structures the engine actually trades it lands at 1.00x market for the
    # morning debit spread (against 0.86x) and 1.43x for the afternoon credit
    # spread (against 13.20x).
    N.fetch_option_chain = lambda *a, **k: {}
    # Size as the STRATEGY would, not as the live order cap allows.
    #
    # nodes.py clamps quantity to TRADING_MAX_ORDER_CONTRACTS while
    # LIVE_ORDERS is on, so the engine tracks what the broker was actually
    # given. Correct live, wrong here: with live orders enabled on the
    # droplet the same configuration that measured +89 a trade came back at
    # +8.48, because every arm was silently sized to one contract instead of
    # seven. A sweep must measure the strategy, not the training-wheels cap
    # that happens to be set on the box it runs on.
    #
    # Found on 2026-08-25 by a total that moved 10x with no parameter change.
    N.tradier_orders.LIVE_ORDERS = False
    if CHAIN_PRICING:
        _patch_pricing()
    # After the pricer, so the grid is applied to whatever prices it produces.
    _patch_ticks()
    # The shadow condor reaches for the chain through data_feed directly, so
    # patching the nodes reference alone left a live HTTP call in every cycle
    # of every sweep -- one per 30-second cache window, plus a warning line
    # per session on any day the market is shut.
    import trading_engine.data_feed as DF
    DF.fetch_option_chain = lambda *a, **k: {}


def _session_vwap(bars_slice: pd.DataFrame) -> float:
    """Volume-weighted average price for THE CURRENT SESSION ONLY.

    Two bugs lived here, and together they broke the harness's entry decisions
    for every sweep ever run against a CLEAN tier.

    ONE: bars_slice is _seen_at(ts) -- the last 390 bars, which is FIVE
    SESSIONS. A "session VWAP" averaged over a week is not one, and on a
    trending week it sits far from the day's own mean, which flips vwap_side
    for hours at a time.

    TWO: it was an unweighted mean of the typical price. Volume was not used
    at all. data_feed.py already records what that costs live -- on
    2026-08-25 the unweighted fallback fired 52 times in one morning and VWAP,
    one of the four terms in clean_bull, ran on it all session.

    MEASURED CONSEQUENCE, QQQ 2026-09-03 at 10:50 ET: live read ABOVE_VWAP and
    opened a CLEAN trade that made +478. The harness read BELOW_VWAP for the
    same bar and took nothing. The other three terms of clean_bull matched
    exactly. Across the nine sessions the engine traded live, the harness
    reproduced two.

    RESIDUAL, AND NOT FIXABLE HERE: yfinance's intraday volume for QQQ is
    unreliable -- data_feed.py measured 200.9M against Tradier's 19.4M for the
    same session, varying between calls. So this is the right SHAPE of number
    computed from a suspect weight, which is much closer than a five-day
    unweighted mean and still not what the engine saw.
    """
    if bars_slice.empty:
        return float("nan")
    day = bars_slice.index[-1].date()
    session = bars_slice[bars_slice.index.map(lambda t: t.date() == day)]
    if session.empty:
        session = bars_slice
    typical = (session["High"] + session["Low"] + session["Close"]) / 3.0
    vol = session["Volume"] if "Volume" in session else None
    if vol is None or float(vol.sum()) <= 0:
        return float(typical.mean())
    return float((typical * vol).sum() / vol.sum())


def _session_state(bars_slice: pd.DataFrame) -> dict:
    """Run the engine's own indicator agents over the bars seen so far."""
    N.fetch_qqq_bars = lambda *a, **k: bars_slice
    # sma_agent calls Tradier for VWAP; compute it from the session's own bars
    # instead, which is what the live VWAP is meant to approximate anyway.
    N.fetch_qqq_session_vwap = lambda: _session_vwap(bars_slice)
    state = {}
    state.update(N.macd_agent({}))
    state.update(N.sma_agent({}))
    state.update(N.bollinger_agent({}))
    state.update(N.rsi_agent({}))
    state["market_sentiment"] = "GOOD"     # see the caveat in the docstring
    state["macro_halt"] = False
    state["buy_more_count"] = 0
    return state


# The full 5-minute series, and how much of it each cycle may see.
#
# The live engine calls fetch_qqq_bars(period="5d"), so a 20-period band or a
# 50 EMA at 10:15 is computed over the previous days as well as this one. The
# first version of this harness handed each session only its OWN bars, which
# left every indicator reading off nine bars at the morning entry and none of
# them warmed up at all -- a squeeze test written against it returned no rows,
# which is how it was noticed.
_HISTORY = None
_LOOKBACK_BARS = 390          # 5 sessions x 78 five-minute bars, as live


def _load_sessions(period: str = "60d") -> dict:
    global _HISTORY
    bars = yf.Ticker("QQQ").history(period=period, interval="5m")
    if bars.empty:
        raise SystemExit("yfinance returned no bars")
    bars = bars.tz_convert(NY)
    _HISTORY = bars
    if CHAIN_PRICING:
        _load_vix(period)
    return {d: g for d, g in bars.groupby(bars.index.date) if len(g) > 40}


def _load_vix(period: str) -> None:
    """One VIX close per replayed session, to scale the smile.

    Daily, not intraday: the smile is a whole-session calibration, and a
    sweep that priced 09:45 off the previous close and 15:45 off the current
    one would be measuring the difference between two feeds rather than a
    parameter. A missing day simply prices at the reference VIX.
    """
    try:
        vix = yf.Ticker("^VIX").history(period=period, interval="1d")
        for ts, close in vix["Close"].items():
            _VIX_BY_DAY[ts.date()] = float(close)
        print(f"VIX loaded for {len(_VIX_BY_DAY)} sessions "
              f"({min(_VIX_BY_DAY.values()):.1f} to {max(_VIX_BY_DAY.values()):.1f})")
    except Exception as exc:
        print(f"VIX unavailable ({exc}) — pricing every session at the reference level.")


def _seen_at(ts) -> pd.DataFrame:
    """Everything the engine would have had at `ts`, warmed up like the live feed."""
    return _HISTORY.loc[:ts].tail(_LOOKBACK_BARS)


def _mark(position, spot: float) -> float:
    if is_credit(position.strategy):
        return fill_price(estimate_credit_value(position.strategy, position.short_strike,
                                                position.long_strike, spot), "buy")
    return fill_price(estimate_spread_value(position.strategy, position.long_strike,
                                            position.short_strike, spot), "sell")


def _stop_pct_for(position):
    """The stop this position is actually managed by, as a negative percent.

    Read from the window that OPENED it, not from whatever window the clock is
    in -- the same rule the engine's own exit ladder follows. Returns None when
    the playbook is unknown, which makes the intrabar check a no-op rather
    than a guess.
    """
    name = (position.playbook or "").split(":")[0]
    for w in PB.WINDOWS:
        if w.name == name:
            return w.stop_loss_pct
    return None


def replay_session(day_bars: pd.DataFrame, start: dtime, end: dtime) -> list:
    """One session through the engine. Returns the trades it closed."""
    _Account.open_session()
    _Cooldown.open_session()
    broker = MockBrokerClient(position=None, available_cash=_Account.equity)
    trades, entry = [], None
    _stall = {"peak": -1e9, "at": None}

    for i in range(len(day_bars)):
        ts = day_bars.index[i]
        if not (start <= ts.time() <= end):
            continue
        _Clock.now = ts.to_pydatetime()

        # Retime the stall for the late session. Assigned onto the nodes module
        # because BOTH engine stall sites -- the ride at 1954 and the credit
        # branch at 2079 -- read the global at call time, so this reaches the
        # rules the engine actually runs rather than a copy of them.
        # ONLY WHEN THE LATE-STALL TEST IS THE ONE RUNNING. replay_session is
        # shared by every sweep, so an unconditional assignment here would
        # overwrite whatever stall knobs the CURRENT sweep had just set -- and
        # sweep_creditstall's whole purpose is to set them. Writing this the
        # unconditional way silently reduced that sweep to a single repeated
        # arm, which is the section 55 failure exactly.
        eff_stall = STALL_MINUTES
        if LATE_STALL_AFTER is not None:
            eff_stall = (LATE_STALL_MINUTES if ts.time() >= LATE_STALL_AFTER
                         else STALL_MINUTES)
            # BOTH stall rules. A position still open at 15:30 is as likely to
            # be the afternoon credit spread as the morning ride, and retiming
            # only one would answer half the question while reading like the
            # whole of it.
            N.STALL_MINUTES = N.CREDIT_STALL_MINUTES = eff_stall

        seen = _seen_at(ts)
        spot = float(seen["Close"].iloc[-1])
        N.fetch_qqq_spot = lambda s=spot: s

        position = broker.get_open_position()
        if position is not None:
            # service.py marks the position and ratchets its peak before the
            # rules run; without both, every exit that reads the peak is blind.
            position.current_net_value = _mark(position, spot)
            # peak_at as well as peak_return_pct. service.py stamps both, and
            # the credit stall (STALL_ON_CREDIT) reads the TIMESTAMP -- without
            # it the rule sees peak_at=None, declines to disarm the target, and
            # every arm of a sweep testing it returns identical numbers. That
            # tell has cost three separate afternoons in this file already.
            if position.return_pct > position.peak_return_pct or position.peak_at is None:
                position.peak_at = _Clock.now
            position.peak_return_pct = max(position.peak_return_pct, position.return_pct)

            # Would the stop have been touched INSIDE this bar? See
            # INTRABAR_STOPS above. Checked before the agent runs, because a
            # stop that fires mid-bar fires before anything the bar close
            # would have decided.
            if INTRABAR_STOPS and entry is not None:
                stop_pct = _stop_pct_for(position)
                if stop_pct is not None and stop_pct < 0:
                    long_side = not is_credit(position.strategy)
                    adverse = float(day_bars["Low"].iloc[i] if long_side
                                    else day_bars["High"].iloc[i])
                    worst = _mark(position, adverse)
                    debit = entry["debit"]
                    worst_pct = ((worst - debit) / debit * 100.0) if debit else 0.0
                    if worst_pct <= stop_pct:
                        # Filled AT the stop, not at the bar's extreme: that is
                        # what a stop checked every ten seconds achieves, and
                        # the extreme is the five-minute answer this exists to
                        # replace.
                        stop_value = debit * (1.0 + stop_pct / 100.0)
                        broker.sell_all(position.underlying)
                        closed = _close(entry, position, spot, ts, "STOP_LOSS")
                        per = (stop_value - debit) - SLIPPAGE_ROUNDTRIP - COMMISSION_ROUNDTRIP
                        closed["pnl"] = round(per * entry["qty"] * 100, 2)
                        closed["pct"] = round(per / debit * 100, 2) if debit else 0.0
                        _Account.book(closed["pnl"])
                        _Cooldown.book(closed["strategy"], closed["pnl"], ts)
                        trades.append(closed)
                        entry = None
                        _stall.update(peak=-1e9, at=None)
                        continue

        try:
            state = _session_state(seen)
        except Exception:
            continue

        before = broker.get_open_position()

        # Stalled-peak check, ahead of the agent so it can pre-empt the ride.
        #
        # RIDING POSITIONS ONLY. This used to run on ANY open position and
        # `continue` when it fired, which meant it SHADOWED the engine's own
        # credit exit branch entirely: STALL_ON_CREDIT, CREDIT_STALL_REQUIRES_ARM
        # and CREDIT_STRIKE_EXIT_BUFFER could never be reached, and a sweep of
        # them returned identical rows at every setting including a $2.00
        # buffer on a $4-wide spread.
        #
        # WORSE, IT LOOKED LIKE IT WORKED. sweep_creditstall's arms did differ
        # from one another -- but only because they also set STALL_MINUTES,
        # which drives THIS rule. The +7.47/day attributed to the credit stall
        # was this copy being retimed. A deployment decision was made on it.
        #
        # The engine implements the stall in both branches itself, and since
        # peak_at is stamped above its own code actually runs now, so this
        # copy is redundant as well as harmful. Kept only for the ride, where
        # the intrabar stop check below also needs to pre-empt the agent.
        _riding = (before is not None
                   and PB.rides_to_close(getattr(before, "playbook", "") or ""))
        if eff_stall > 0 and _riding and entry is not None:
            r = before.return_pct
            if r > _stall["peak"]:
                _stall["peak"], _stall["at"] = r, ts
            elif (_stall["at"] is not None
                  and (ts - _stall["at"]).total_seconds() / 60.0 >= eff_stall
                  and r <= _stall["peak"] - STALL_GIVEBACK_PCT):
                broker.sell_all(before.underlying)
                closed = _close(entry, before, spot, ts, "STALL")
                _Account.book(closed["pnl"])
                _Cooldown.book(closed["strategy"], closed["pnl"], ts)
                trades.append(closed)
                entry = None
                _stall.update(peak=-1e9, at=None)
                continue

        out = N.execution_risk_agent(state, broker=broker)
        after = broker.get_open_position()

        if before is None and after is not None:
            entry = {"ts": ts, "strategy": after.strategy, "qty": after.quantity,
                     "debit": after.entry_net_debit, "playbook": after.playbook,
                     # The zone reading AT THE MOMENT OF ENTRY. _close does
                     # {**entry, ...}, so these ride into the trade record and
                     # sweep_zones can bucket outcomes by them without a second
                     # replay. Recorded for every arm, read by one.
                     "zone": state.get("zone"),
                     "zone_extension": state.get("zone_extension"),
                     "day_range_pos_pct": state.get("day_range_pos_pct"),
                     "prior_change_pct": state.get("prior_change_pct"),
                     "gap_pct": state.get("gap_pct"),
                     "dist_day_high_pct": state.get("dist_day_high_pct"),
                     "dist_day_low_pct": state.get("dist_day_low_pct")}
            _stall.update(peak=-1e9, at=None)
        elif before is not None and after is None and entry is not None:
            closed = _close(entry, before, spot, ts, out.get("exit_reason", ""))
            _Account.book(closed["pnl"])
            _Cooldown.book(closed["strategy"], closed["pnl"], ts)
            trades.append(closed)
            entry = None
            if STAND_DOWN_AFTER_RIDE_RATCHET and closed["reason"] == "RATCHET":
                # Nothing is open, so ending the day IS standing down.
                break

    position = broker.get_open_position()
    if position is not None and entry is not None:
        spot = float(day_bars["Close"].iloc[-1])
        closed = _close(entry, position, spot, day_bars.index[-1], "EOD")
        _Account.book(closed["pnl"])
        trades.append(closed)
    return trades



def _run_arm(sessions: dict, start: dtime, end: dtime = dtime(15, 45)):
    """One configuration across every session, from a clean account.

    Every sweep arm goes through here. The alternative -- each sweep looping
    over sessions itself -- is how several arms ended up inheriting the
    previous arm's compounded equity, which made a parameter look responsible
    for P&L that was really just a larger starting balance. The giveback
    sweep reported +32.50 to +58.58 a trade across four arms whose exit mixes
    were identical to the trade, which is what gave it away.
    """
    _Account.equity = EQUITY
    trades, per_day = [], []
    for day, bars in sessions.items():
        day_trades = replay_session(bars, start, end)
        trades += day_trades
        per_day.append({"day": day, "pnl": sum(t["pnl"] for t in day_trades),
                        "trades": len(day_trades)})
    return trades, per_day

def _close(entry: dict, position, spot: float, ts, reason: str) -> dict:
    exit_value = _mark(position, spot)
    per = ((entry["debit"] - exit_value) if is_credit(entry["strategy"])
           else (exit_value - entry["debit"]))
    per -= SLIPPAGE_ROUNDTRIP + COMMISSION_ROUNDTRIP
    return {**entry, "exit_ts": ts, "exit_value": exit_value,
            "pnl": round(per * entry["qty"] * 100, 2),
            "pct": round(per / entry["debit"] * 100, 2) if entry["debit"] else 0.0,
            "reason": reason}


def _report(label: str, trades: list, sessions: int):
    if not trades:
        print(f"  {label:32s} no trades in {sessions} sessions")
        return
    wins = [t for t in trades if t["pnl"] > 0]
    total = sum(t["pnl"] for t in trades)
    reasons = {}
    for t in trades:
        reasons[t["reason"]] = reasons.get(t["reason"], 0) + 1
    # Split halves, because an average that reverses between them is a result
    # about two samples rather than about the parameter.
    half = len(trades) // 2
    h1 = sum(t["pnl"] for t in trades[:half]) / max(half, 1)
    h2 = sum(t["pnl"] for t in trades[half:]) / max(len(trades) - half, 1)
    print(f"  {label:32s} {len(trades):3d} tr  {len(wins) / len(trades) * 100:3.0f}% win  "
          f"{total / len(trades):+8.2f}/tr  {total:+9.2f} tot  "
          f"worst {min(t['pnl'] for t in trades):+8.2f}  halves {h1:+7.2f}/{h2:+7.2f}  {reasons}")


def sweep_morning(sessions: dict):
    print("\nMORNING_DRIFT -- width x long-leg depth, entries 10:15-11:30\n")
    base = PB.WINDOWS
    # depth positions the LONG leg below the money; width then places the
    # short leg above it. depth 2 / width 10 is live ($2 ITM, $8 OTM);
    # depth 5 / width 8 is the $5 ITM / $3 OTM placement.
    for width in (6.0, 8.0, 10.0):
        for depth in (2.0, 3.0, 5.0):
            PB.WINDOWS = tuple(
                replace(w, width=width, long_depth=depth) if w.name == "MORNING_DRIFT" else w
                for w in base
            )
            PB.ENABLED_WINDOWS = frozenset({"MORNING_DRIFT"})
            trades, _ = _run_arm(sessions, dtime(10, 15))
            depth_label = "deep (long = width)" if depth is None else f"long ${depth:.0f} ITM"
            _report(f"${width:.0f} wide, {depth_label}", trades, len(sessions))
    PB.WINDOWS = base


def sweep_windows(sessions: dict):
    """Each window traded alone, so no window can hide inside another's P&L.

    Read the credit row with the caveat in mind: it sells far-out-of-the-money
    premium, which is exactly where broker.py's model was measured wrong on
    2026-08-21 -- 0.30 modelled against 0.02 quoted. There are no historical
    chains to replay, so a backtest of a premium-selling window cannot be
    trusted in absolute terms. Its forward record, priced from the chain since
    2026-08-21, is the evidence that counts.
    """
    print("")
    print("EVERY WINDOW SOLO -- 60 sessions")
    print("")
    base = PB.WINDOWS
    for w in base:
        PB.ENABLED_WINDOWS = frozenset({w.name})
        trades, _ = _run_arm(sessions, w.start)
        # The warning only applies to the old pricer. Under SWEEP_CHAIN_PRICING
        # a credit row is the one that got MORE trustworthy, not less -- that
        # is the whole point of the flag -- and leaving the label on would
        # have the reader discount the row that finally means something.
        flag = ("" if CHAIN_PRICING
                else "  [MODEL-PRICED, see docstring]" if w.placement == "CREDIT" else "")
        _report(f"{w.name} ${w.width:.0f} {w.placement}{flag}", trades, len(sessions))
    PB.WINDOWS = base


def sweep_daydrop(sessions: dict):
    """Does the morning long want the session to have FALLEN first?

    playbook.py already records that morning declines in QQQ tend to reverse,
    and uses it to explain why a put debit spread and a bear call spread both
    fail in that window. This is the bullish half of the same fact, which was
    never tested: take the CLEAN long only when the day has already been down
    at least N% at its worst.

    A crude harness put the effect at +55.45 a trade above a 1.0% drawdown
    against -12.56 below it, on six trades -- and scored all trades at -2.10
    where the engine scores +122.62, so it was splitting a P&L it could not
    reproduce. This runs the real engine.

    WATCH THE ROW COUNT, not just the P&L. A gate that admits nothing prints
    a beautiful average over two trades.
    """
    print("")
    print("MORNING DRAWDOWN GATE -- require the session to have been down first")
    print(f"  pricing: {'CHAIN-CALIBRATED' if CHAIN_PRICING else 'MODEL'}")
    print("")
    base = PB.WINDOWS
    for drawdown in (None, 0.3, 0.5, 0.7, 0.8, 1.0, 1.25):
        PB.WINDOWS = tuple(
            replace(w, min_session_drawdown_pct=drawdown) if w.name == "MORNING_DRIFT" else w
            for w in base
        )
        PB.ENABLED_WINDOWS = frozenset({"MORNING_DRIFT"})
        trades, per_day = _run_arm(sessions, dtime(9, 45))
        label = "no gate (live)" if drawdown is None else f"had been down >= {drawdown:.2f}%"
        _report(label, trades, len(sessions))
    PB.WINDOWS = base


def sweep_target(sessions: dict):
    """A fixed profit target against the ride-and-ratchet the window uses.

    MORNING_DRIFT sets ride_to_close, so take_profit_pct is retained but never
    consulted -- the ratchet arms at +32% and exits on a 20% giveback instead.
    A crude harness (no ratchet at all) liked a fixed +40% target: -11.10 a
    trade riding, +8.24 with the target, halves +9.94/+6.54.

    That harness's "ride" arm is a strawman, because riding WITHOUT a ratchet
    is not what the engine does. If the target only beats an unratcheted ride
    it has discovered the ratchet, not a better exit. This runs both against
    the real thing.
    """
    print("")
    print("EXIT STYLE -- fixed target vs ride-and-ratchet")
    print(f"  pricing: {'CHAIN-CALIBRATED' if CHAIN_PRICING else 'MODEL'}")
    print("")
    base = PB.WINDOWS
    arms = [(True, None, "ride + ratchet (live)")]
    for tp in (20.0, 30.0, 40.0, 50.0, 60.0):
        arms.append((False, tp, f"fixed target +{tp:.0f}%, no ride"))
    for ride, tp in ((True, None),):
        pass
    for ride, tp, label in arms:
        PB.WINDOWS = tuple(
            replace(w, ride_to_close=ride, take_profit_pct=(tp if tp else w.take_profit_pct))
            if w.name == "MORNING_DRIFT" else w
            for w in base
        )
        PB.ENABLED_WINDOWS = frozenset({"MORNING_DRIFT"})
        trades, _ = _run_arm(sessions, dtime(9, 45))
        _report(label, trades, len(sessions))
    PB.WINDOWS = base


def sweep_creditstop(sessions: dict):
    """Is the credit window's stop protecting anything, or just realising noise?

    AFTERNOON_CREDIT inherited stop_loss_pct=-100 from the debit side, where
    "-100% of premium paid" is a total loss and means something. On a CREDIT
    position -100% means the spread doubled -- and when the credit is sixteen
    cents, doubling is thirty-two cents, which a 67-cent move in QQQ produces
    without ever touching the short strike.

    That is not hypothetical. Live on 2026-08-25 the engine sold 711/715 for
    0.16 at 14:34 and bought it back at 0.39 at 14:46 for -$23. QQQ's high
    for the rest of the session was 710.67 against a 711 short strike: the
    spread would have expired worthless and kept the whole credit.

    A credit spread's loss is ALREADY capped at width-minus-credit by the long
    leg. The stop does not lower that ceiling; it converts unrealised noise
    into realised loss. Crude harness over 60 sessions, breach rate identical
    at 29% in every arm:

        2x credit (live)   58% win  29% stopped   -9.00/tr
        3x credit          67% win  20% stopped   -6.22/tr
        no stop            75% win   0% stopped   +0.97/tr
    """
    print("")
    print("CREDIT STOP -- what the buy-it-back stop actually buys")
    print(f"  pricing: {'CHAIN-CALIBRATED' if CHAIN_PRICING else 'MODEL'}")
    print("")
    base = PB.WINDOWS
    for stop, risk_off, label in (
        (-100.0, -60.0, "stop -100%, risk-off -60% (live)"),
        (-200.0, -60.0, "stop -200%, risk-off -60%"),
        (-300.0, -60.0, "stop -300%, risk-off -60%"),
        (-100.0, None, "stop -100%, NO risk-off"),
        (-400.0, -60.0, "stop -400%, risk-off -60%"),
        (-600.0, -60.0, "stop -600%, risk-off -60%"),
        # -10000 was tried here and returned NO TRADES in 60 sessions. Risk
        # sizing divides the risk budget by the stop distance, so an absurd
        # stop makes every contract look infinitely risky and sizes to zero.
        # An arm that takes no trades is not "the no-stop case", it is a
        # broken arm, and it prints a blank row rather than a warning.
    ):
        PB.WINDOWS = tuple(
            replace(w, stop_loss_pct=stop, risk_off_pct=risk_off)
            if w.name == "AFTERNOON_CREDIT" else w
            for w in base
        )
        PB.ENABLED_WINDOWS = frozenset({"AFTERNOON_CREDIT"})
        trades, _ = _run_arm(sessions, dtime(13, 30))
        _report(label, trades, len(sessions))
    PB.WINDOWS = base


def sweep_mincredit(sessions: dict):
    """How much premium is worth getting out of bed for?

    TRADING_MIN_CREDIT exists and has sat at its 0.05 default. On 2026-08-27
    the live window sold 721/723 twice at exactly 0.05 -- ON the floor, since
    the guard is a strict `<` -- and the second one ratcheted out at a
    measured +20% for exactly $0.00. At a nickel of credit one tick IS 20%,
    so every exit threshold in the window sits below the resolution of the
    price grid. The engine was measuring a quantity it could not act on.

    This arm asks where the floor should be. It is run against the DEPLOYED
    configuration -- 13:30 start, $2 wing, 0.25 short delta -- not the file
    defaults, because the answer is a number to put in the live env.

    READ THE TRADE COUNT ALONGSIDE THE AVERAGE. A higher floor is a filter,
    and a filter that improves the per-trade average by refusing everything
    has not found an edge, it has found the door. The per-session line is the
    one that can tell those apart.

    And read this arm the way section 16 says to read every entry filter: it
    RANKS floors. The harness holds one contract and has no ratchet, so the
    dollar figures are not what the live window would earn.
    """
    print("")
    print("CREDIT FLOOR -- minimum premium worth taking width-of-risk for")
    print(f"  pricing: {'CHAIN-CALIBRATED' if CHAIN_PRICING else 'MODEL'}"
          f"    grid: {'PENNY/NICKEL TICKS' if TICK_PRICING else 'SMOOTH -- not real'}")
    print("")
    base_windows, base_floor, base_delta = PB.WINDOWS, N.MIN_CREDIT, N.CREDIT_SHORT_DELTA
    # Match the live env, so the number this prints can be pasted into it.
    N.CREDIT_SHORT_DELTA = 0.25
    PB.WINDOWS = tuple(
        replace(w, start=dtime(13, 30), width=2.0) if w.name == "AFTERNOON_CREDIT" else w
        for w in base_windows
    )
    PB.ENABLED_WINDOWS = frozenset({"AFTERNOON_CREDIT"})
    try:
        for floor in (0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50):
            N.MIN_CREDIT = floor
            trades, per_day = _run_arm(sessions, dtime(13, 30))
            traded = len([d for d in per_day if d["trades"]])
            _report(f"floor {floor:.2f}  ({traded}/{len(sessions)} sessions)",
                    trades, len(sessions))
    finally:
        PB.WINDOWS, N.MIN_CREDIT, N.CREDIT_SHORT_DELTA = base_windows, base_floor, base_delta


def sweep_strength(sessions: dict):
    """Should a call credit spread be cut faster when the tape keeps making highs?

    The proposal, 2026-08-27: a bullish session is where a call credit spread
    gets hurt, so exit early when QQQ keeps printing new highs rather than
    waiting for a percentage stop.

    It is worth separating from the stop question sweep_creditstop already
    answered. That one found tightening UNCONDITIONALLY makes things worse --
    2x credit -9.00/tr, 3x -6.22, no stop +0.97 -- because a percentage stop
    on a credit spread realises noise that the long leg has already capped.
    This asks something different: not "stop sooner" but "stop on a SIGNAL",
    where the signal is the tape doing the specific thing that hurts.

    Both readings of "keeps hitting highs" are tested, because they are not
    the same rule:

        session high   spot exceeds the session's high as it stood at entry.
                       One clean event, and the one a trader watching the
                       tape would actually notice.
        three up bars  three consecutive rising closes. Fires more often and
                       earlier, and catches a grind that never prints a
                       dramatic new high.

    Placement is held constant across every arm -- same strikes, same entry
    minute, same session set -- so the exit rule is the only thing moving.

    RANK, DO NOT SIZE. One contract, no ratchet, no re-entry. Section 16.
    """
    print("")
    print("STRENGTH EXIT -- cut the call credit spread when the tape makes highs?")
    print(f"  pricing: {'CHAIN-CALIBRATED' if CHAIN_PRICING else 'MODEL'}"
          f"    grid: {'PENNY/NICKEL TICKS' if TICK_PRICING else 'SMOOTH -- not real'}")
    print("")
    offset, wing, entry = 2.0, 2.0, dtime(13, 30)

    def run(rule):
        out = []
        for day, bars in sessions.items():
            idx = [i for i, ts in enumerate(bars.index) if ts.time() >= entry]
            if not idx:
                continue
            i = idx[0]
            spot = float(bars["Close"].iloc[i])
            atm = broker_mod.round_to_strike(spot)
            short, long_ = atm + offset, atm + offset + wing
            mins = broker_mod.minutes_to_expiry(bars.index[i].to_pydatetime())
            credit = fill_price(
                estimate_credit_value(CALL_CREDIT_SPREAD, short, long_, spot, mins), "sell")
            if credit <= 0.02:
                continue
            # The session high as it stood when the position was opened.
            high_col = "High" if "High" in bars else "Close"
            high_at_entry = float(bars[high_col].iloc[:i + 1].max())
            ups, pnl, reason = 0, None, None
            prev = spot
            for j in range(i + 1, len(bars)):
                ts = bars.index[j]
                if ts.time() > dtime(15, 45):
                    break
                sp = float(bars["Close"].iloc[j])
                m = broker_mod.minutes_to_expiry(ts.to_pydatetime())
                cost = fill_price(
                    estimate_credit_value(CALL_CREDIT_SPREAD, short, long_, sp, m), "buy")
                ups = ups + 1 if sp > prev else 0
                prev = sp
                pnl, reason = (credit - cost) * 100, "FORCE_CLOSE"
                if cost <= credit * 0.50:
                    reason = "TARGET"
                    break
                if rule == "stop2" and cost >= credit * 2.0:
                    reason = "STOP"
                    break
                if rule == "stop6" and cost >= credit * 6.0:
                    reason = "STOP"
                    break
                if rule == "newhigh" and float(bars[high_col].iloc[j]) > high_at_entry:
                    reason = "NEW_HIGH"
                    break
                if rule == "up3" and ups >= 3:
                    reason = "THREE_UP"
                    break
            if pnl is not None:
                out.append({"pnl": round(pnl, 2), "reason": reason,
                            "pct": round((pnl / 100) / credit * 100, 2)})
        return out

    for rule, label in (
        (None, "hold to close, no stop"),
        ("stop6", "stop at 6x credit (live)"),
        ("stop2", "stop at 2x credit"),
        ("newhigh", "exit on new session high"),
        ("up3", "exit on 3 rising bars"),
    ):
        _report(label, run(rule), len(sessions))


def sweep_package(sessions: dict):
    """The 2026-08-27 proposal, as a package and component by component.

    Proposed after a session where the morning stopped out at 11:39 on the
    dip to 716.67 -- the local bottom -- and QQQ then ran to 720.53 by 13:15:

        placement   $5 ITM / $3 OTM instead of $2 ITM / $8 OTM
        entry       2 contracts, one now and one ten minutes later
        target      book a clean 70%
        exit        otherwise hold into the afternoon
        stop        -10%, and retry after 30 minutes

    The 30-minute retry is already the deployed behaviour
    (TRADING_REENTRY_COOLDOWN_MINUTES=30) and is already simulated here, so
    it is not a variable -- it is the baseline both arms sit on.

    ISOLATED, NOT JUST BUNDLED. A package that loses tells you nothing about
    which part lost, and a package that wins can win despite one of its
    parts. Each component is therefore also run alone against live, which is
    the only way to see whether the pieces interact or simply add up.

    RANK, DO NOT SIZE (section 16). One position at a time, harness friction.
    """
    print("")
    print("THE PACKAGE -- proposed morning configuration, and each part alone")
    print(f"  pricing: {'CHAIN-CALIBRATED' if CHAIN_PRICING else 'MODEL'}"
          f"    grid: {'PENNY/NICKEL TICKS' if TICK_PRICING else 'SMOOTH -- not real'}")
    print("")
    base = PB.WINDOWS
    base_slices, base_mins = N.ENTRY_SLICES, N.ENTRY_SLICE_MINUTES

    # (label, width, depth, stop, final_tp, slices)
    arms = (
        ("live: $10/$2 ITM, -20%, ride", 10.0, 2.0, -20.0, None, 1),
        ("PACKAGE (all five)",            8.0, 5.0, -10.0,  70.0, 2),
        ("  package, live placement",    10.0, 2.0, -10.0,  70.0, 2),
        ("  package, live stop -20%",     8.0, 5.0, -20.0,  70.0, 2),
        ("  package, no 70% target",      8.0, 5.0, -10.0, None, 2),
        ("  package, single entry",       8.0, 5.0, -10.0,  70.0, 1),
        ("  live + 70% target only",     10.0, 2.0, -20.0,  70.0, 1),
        ("  live + 2 slices only",       10.0, 2.0, -20.0, None, 2),
        ("  live + -10% stop only",      10.0, 2.0, -10.0, None, 1),
        # Is -10% a real optimum or one lucky cell? Scanned on live
        # placement, everything else held at live.
        ("stop scan: -5%",               10.0, 2.0,  -5.0, None, 1),
        ("stop scan: -8%",               10.0, 2.0,  -8.0, None, 1),
        ("stop scan: -12%",              10.0, 2.0, -12.0, None, 1),
        ("stop scan: -15%",              10.0, 2.0, -15.0, None, 1),
        ("stop scan: -25%",              10.0, 2.0, -25.0, None, 1),
    )
    try:
        for label, width, depth, stop, final_tp, slices in arms:
            PB.WINDOWS = tuple(
                replace(w, width=width, long_depth=depth, stop_loss_pct=stop,
                        final_take_profit_pct=final_tp)
                if w.name == "MORNING_DRIFT" else w
                for w in base
            )
            N.ENTRY_SLICES, N.ENTRY_SLICE_MINUTES = slices, 10.0
            PB.ENABLED_WINDOWS = frozenset({"MORNING_DRIFT"})
            trades, _ = _run_arm(sessions, dtime(10, 15))
            _report(label, trades, len(sessions))
    finally:
        PB.WINDOWS = base
        N.ENTRY_SLICES, N.ENTRY_SLICE_MINUTES = base_slices, base_mins


def sweep_creditcfg(sessions: dict):
    """What settings should AFTERNOON_CREDIT run tomorrow?

    The window stays on by decision (2026-08-27), so the question is no
    longer whether but how. Four knobs interact and have only ever been moved
    one at a time: entry time, wing width, short delta, and the credit floor.

    Everything measured on this window so far says the same thing -- the
    playbook's own comment block, the floor sweep, the strength sweep -- so
    the honest expectation is that every cell loses and the output is a
    RANKING of least-bad. That is still worth having: a config that loses
    $4 a trade and one that loses $64 are not the same decision.

    The floor matters most and for a mechanical reason. At a nickel of credit
    one penny is 20%, so every exit threshold sits below the resolution of
    the price grid -- the live 721/723 on 2026-08-27 ratcheted out at a
    measured +20% for exactly $0.00. A floor is what buys the exit logic room
    to express a decision at all.
    """
    print("")
    print("CREDIT CONFIG -- entry time x width x floor, on the deployed delta")
    print(f"  pricing: {'CHAIN-CALIBRATED' if CHAIN_PRICING else 'MODEL'}"
          f"    grid: {'PENNY/NICKEL TICKS' if TICK_PRICING else 'SMOOTH -- not real'}")
    print("")
    base_windows, base_floor = PB.WINDOWS, N.MIN_CREDIT
    try:
        for start_h, start_m in ((13, 30), (14, 0)):
            for width in (2.0, 4.0):
                for floor in (0.05, 0.20):
                    N.MIN_CREDIT = floor
                    PB.WINDOWS = tuple(
                        replace(w, start=dtime(start_h, start_m), width=width)
                        if w.name == "AFTERNOON_CREDIT" else w
                        for w in base_windows
                    )
                    PB.ENABLED_WINDOWS = frozenset({"AFTERNOON_CREDIT"})
                    trades, per_day = _run_arm(sessions, dtime(start_h, start_m))
                    traded = len([d for d in per_day if d["trades"]])
                    _report(f"{start_h:02d}:{start_m:02d} ${width:.0f} wide floor {floor:.2f} "
                            f"({traded}/{len(sessions)}d)", trades, len(sessions))
    finally:
        PB.WINDOWS, N.MIN_CREDIT = base_windows, base_floor


def sweep_trendstop(sessions: dict):
    """Should the morning stop defer to the trend instead of firing on price?

    Asked 2026-08-27, after a session that stopped out at 11:39 on the dip to
    716.67 -- the local bottom -- and ran to 720.53 by 13:15. The idea: if the
    longer moving average is still up, the dip is noise and the stop should
    wait for the TREND to break rather than for the position to fall a fixed
    percentage.

    Two readings of "trend still up", because they behave differently:

        ema_cross   the 9 EMA is still above the 20 SMA. Structural, slower
                    to flip, so it holds the stop off through deeper dips.
        ema9        price is still above the 9 EMA. Faster, releases the stop
                    back almost immediately on a real turn.

    Swept at three stop widths, because a trend guard and a stop width are
    not independent: a guard that holds through dips may make a TIGHTER stop
    affordable, which is the interesting version of the question. A guard
    tested only at the deployed width would miss that entirely.

    The downside stays bounded -- a debit spread's loss is capped at the
    premium, the 13:25 ride deadline and force close still end the trade, and
    the risk-off rule still owns its band when macro turns BAD.
    """
    print("")
    print("TREND-DEFERRED STOP -- fire on price, or wait for the trend to break?")
    print(f"  pricing: {'CHAIN-CALIBRATED' if CHAIN_PRICING else 'MODEL'}"
          f"    grid: {'PENNY/NICKEL TICKS' if TICK_PRICING else 'SMOOTH -- not real'}")
    print("")
    base = PB.WINDOWS
    try:
        for stop in (-10.0, -20.0, -5.0):
            for guard in (None, "ema_cross", "ema9"):
                PB.WINDOWS = tuple(
                    replace(w, stop_loss_pct=stop, stop_defers_to_trend=guard)
                    if w.name == "MORNING_DRIFT" else w
                    for w in base
                )
                PB.ENABLED_WINDOWS = frozenset({"MORNING_DRIFT"})
                trades, _ = _run_arm(sessions, dtime(10, 15))
                label = f"stop {stop:+.0f}% guard {guard or 'none (live)'}"
                _report(label, trades, len(sessions))
            print("")
    finally:
        PB.WINDOWS = base


def sweep_handoff(sessions: dict):
    """Should the morning winner still hand its slot to the credit window?

    The 13:25 handoff exists because the engine holds one position at a time,
    and it was justified on these numbers:

        morning rides to 15:45, credit never trades   +36.40 a day
        morning hands over at 13:25, credit trades    +80.87 a day

    Both were MODEL-PRICED, and the model overstates a far-OTM credit spread
    by up to 18x (see trading_engine/chain_pricer.py). The handoff gives up a
    riding morning position to buy a credit trade -- so if the credit trade
    is not worth 63 dollars a day, the trade that justified the cut was
    priced on premium that was never there.

    That is the whole question here, and it is only answerable with
    SWEEP_CHAIN_PRICING=1. Run it both ways: the arms should DIVERGE, and if
    they do not, the flag is not reaching the pricer.
    """
    print("")
    print("13:25 HANDOFF -- does giving the slot to the credit window still pay?")
    print(f"  pricing: {'CHAIN-CALIBRATED' if CHAIN_PRICING else 'MODEL (see docstring)'}")
    print("")
    base = PB.WINDOWS
    for ride_until, windows, label in (
        (dtime(13, 25), {"MORNING_DRIFT", "AFTERNOON_CREDIT"}, "hand over at 13:25, credit trades"),
        (None, {"MORNING_DRIFT", "AFTERNOON_CREDIT"}, "ride to 15:45, credit gets what is left"),
        (None, {"MORNING_DRIFT"}, "ride to 15:45, credit window OFF"),
        (dtime(13, 25), {"MORNING_DRIFT"}, "hand over at 13:25, credit window OFF"),
    ):
        PB.WINDOWS = tuple(
            replace(w, ride_until=ride_until) if w.name == "MORNING_DRIFT" else w
            for w in base
        )
        PB.ENABLED_WINDOWS = frozenset(windows)
        _, per_day = _run_arm(sessions, dtime(9, 45))
        _report_daily(label, per_day)
    PB.WINDOWS = base


def sweep_retries(sessions: dict):
    """Does letting the morning window stay open pay for a second entry?

    Today's move went 709.40 at 10:15 to 715 by 11:50 and the engine caught
    it at 11:01, when the fourth confirmation finally arrived. It then rode
    to the 13:25 handoff. Nothing could re-enter, because MORNING_DRIFT
    closes at 11:30 -- so a setup that re-fires at noon has no window to fire
    into. This sweeps that end time.
    """
    print("")
    print("MORNING WINDOW LENGTH -- how late may a debit entry still open?")
    print("")
    base = PB.WINDOWS
    for end in (dtime(11, 30), dtime(12, 30), dtime(13, 0), dtime(13, 25)):
        PB.WINDOWS = tuple(
            replace(w, end=end) if w.name == "MORNING_DRIFT" else w for w in base
        )
        PB.ENABLED_WINDOWS = frozenset({"MORNING_DRIFT"})
        trades, _ = _run_arm(sessions, dtime(10, 15))
        _report(f"entries until {end.strftime('%H:%M')}", trades, len(sessions))
    PB.WINDOWS = base


def sweep_mstart(sessions: dict):
    """May MORNING_DRIFT open at 09:45 instead of 10:15?

    Asked 2026-08-28. The 09:45-10:15 half hour is empty -- ATM_MOMENTUM is
    defined there and switched off -- and 09:45 is the earliest an entry can
    legally fire, since _is_within_opening_warmup() refuses every entry before
    it. So this is a start time with nothing in its way, not a new window.

    TWO FRAMES, AND THE SECOND IS THE ONE THAT DECIDES. An earlier start can
    only add trades, and section 16 caught five separate variants that added
    trades and lost money doing it. Per-trade P&L alone would let an arm look
    good while earning less per DAY, so both are printed and the daily block
    is the verdict.

    RUN WITH THE CREDIT WINDOW TOO. MORNING_DRIFT rides to the 13:25 handoff
    and there is one position slot, so an earlier entry does not just add a
    trade -- it occupies the slot sooner and can push the whole day around.
    The solo block isolates the start time; the combined block is what would
    actually run.
    """
    print("")
    print("MORNING_DRIFT START TIME -- 09:45 is the earliest the warmup allows")
    print(f"  pricing: {'CHAIN-CALIBRATED' if CHAIN_PRICING else 'MODEL (see docstring)'}")
    print("")
    base = PB.WINDOWS
    starts = (dtime(9, 45), dtime(10, 0), dtime(10, 15), dtime(10, 30), dtime(11, 0))

    print("  MORNING_DRIFT SOLO -- per trade")
    for start in starts:
        PB.WINDOWS = tuple(
            replace(w, start=start) if w.name == "MORNING_DRIFT" else w for w in base
        )
        PB.ENABLED_WINDOWS = frozenset({"MORNING_DRIFT"})
        trades, per_day = _run_arm(sessions, start)
        label = f"opens {start.strftime('%H:%M')}"
        _report(label + (" (live)" if start == dtime(10, 15) else ""), trades, len(sessions))

    print("")
    print("  MORNING_DRIFT SOLO -- per day")
    for start in starts:
        PB.WINDOWS = tuple(
            replace(w, start=start) if w.name == "MORNING_DRIFT" else w for w in base
        )
        PB.ENABLED_WINDOWS = frozenset({"MORNING_DRIFT"})
        _, per_day = _run_arm(sessions, start)
        _report_daily(f"opens {start.strftime('%H:%M')}" +
                      (" (live)" if start == dtime(10, 15) else ""), per_day)

    print("")
    print("  BOTH LIVE WINDOWS -- per day, which is what would run")
    for start in starts:
        PB.WINDOWS = tuple(
            replace(w, start=start) if w.name == "MORNING_DRIFT" else w for w in base
        )
        PB.ENABLED_WINDOWS = frozenset({"MORNING_DRIFT", "AFTERNOON_CREDIT"})
        _, per_day = _run_arm(sessions, start)
        _report_daily(f"opens {start.strftime('%H:%M')}" +
                      (" (live)" if start == dtime(10, 15) else ""), per_day)

    PB.WINDOWS = base


def _report_daily(label: str, per_day: list):
    """Per SESSION, not per trade -- the only frame in which "more trades"
    can be judged, since a rule that adds trades usually adds worse ones."""
    if not per_day:
        print(f"  {label:34s} nothing traded")
        return
    days = len(per_day)
    total = sum(d["pnl"] for d in per_day)
    trades = sum(d["trades"] for d in per_day)
    green = len([d for d in per_day if d["pnl"] > 0])
    half = days // 2
    h1 = sum(d["pnl"] for d in per_day[:half]) / max(half, 1)
    h2 = sum(d["pnl"] for d in per_day[half:]) / max(days - half, 1)
    print(f"  {label:34s} {days:2d} days  {trades / days:4.2f} tr/day  "
          f"{total / days:+8.2f}/day  {total:+9.2f} tot  {green / days * 100:3.0f}% green days  "
          f"worst {min(d['pnl'] for d in per_day):+8.2f}  halves {h1:+7.2f}/{h2:+7.2f}")


def _run_one_side(bars, i: int, offset: float, wing: float, calls: bool = True):
    """One credit vertical, priced and exited exactly like _run_condor's."""
    spot = float(bars["Close"].iloc[i])
    atm = round_to_strike(spot)
    if calls:
        short, long_ = atm + offset, atm + offset + wing
        strat = CALL_CREDIT_SPREAD
    else:
        short, long_ = atm - offset, atm - offset - wing
        strat = PUT_CREDIT_SPREAD
    mins = broker_mod.minutes_to_expiry(bars.index[i].to_pydatetime())
    credit = fill_price(estimate_credit_value(strat, short, long_, spot, mins), "sell")
    if credit <= 0.02:
        return None
    pnl, reason = None, None
    for j in range(i + 1, len(bars)):
        ts = bars.index[j]
        if ts.time() > dtime(15, 45):
            break
        sp = float(bars["Close"].iloc[j])
        m = broker_mod.minutes_to_expiry(ts.to_pydatetime())
        cost = fill_price(estimate_credit_value(strat, short, long_, sp, m), "buy")
        pnl, reason = (credit - cost) * 100, "FORCE_CLOSE"
        if cost <= credit * 0.10:
            reason = "TARGET"
            break
        if cost >= credit * 2.0:
            reason = "STOP"
            break
    if pnl is None:
        return None
    return {"pnl": round(pnl, 2), "reason": reason, "pct": round((pnl / 100) / credit * 100, 2)}


def sweep_events(sessions: dict):
    """What the engine earns on scheduled macro days, and what a blackout costs.

    Standing aside is not free -- eight FOMC days a year at the engine's
    average is real money forgone -- so the blackout ships off until these
    sessions are shown to be worse than ordinary ones.
    """
    import trading_engine.macro_calendar as CAL
    print("")
    print("SCHEDULED MACRO DAYS -- FOMC statement days in the sample")
    print("")
    PB.ENABLED_WINDOWS = frozenset({"MORNING_DRIFT", "AFTERNOON_CREDIT"})
    _, per_day = _run_arm(sessions, dtime(9, 45))
    event, ordinary = [], []
    for d in per_day:
        (event if CAL.is_event_day(d["day"]) else ordinary).append(d)
    for label, group in (("event days", event), ("every other day", ordinary)):
        if not group:
            print(f"  {label:18s} none in sample")
            continue
        total = sum(d["pnl"] for d in group)
        green = len([d for d in group if d["pnl"] > 0])
        print(f"  {label:18s} n={len(group):2d}  {total / len(group):+9.2f}/day  "
              f"{green}/{len(group)} green  worst {min(d['pnl'] for d in group):+9.2f}")
    for d in event:
        print(f"      {d['day']}  {d['pnl']:+9.2f}  {d['trades']} trade(s)")

    print("")
    print("  blackout variants:")
    base_mode = CAL.EVENT_BLACKOUT
    for mode in ("off", "afternoon", "day"):
        CAL.EVENT_BLACKOUT = mode
        _, per = _run_arm(sessions, dtime(9, 45))
        total = sum(d["pnl"] for d in per)
        print(f"    {mode:10s} {total / len(per):+9.2f}/day   {total:+10.2f} total")
    CAL.EVENT_BLACKOUT = base_mode


def sweep_forceclose(sessions: dict):
    """How late to flatten. Expiry is 16:15, so this is not the bell.

    15:45 was chosen to leave half an hour of contract life, so the exit
    prices with real time value instead of marking to intrinsic, and to stay
    clear of the market-on-close imbalances that widen quotes and spike gamma
    in the final half hour. The counter-claim is that QQQ tends to move into
    the close and the engine is out before it.

    Both are assertions until swept, and the force close is the exit that
    ends the most trades in the credit window.
    """
    print("")
    print("FORCE CLOSE TIME -- expiry is 16:15")
    print("")
    PB.ENABLED_WINDOWS = frozenset({"MORNING_DRIFT", "AFTERNOON_CREDIT"})
    base_h, base_m = N.FORCE_CLOSE_HOUR, N.FORCE_CLOSE_MINUTE
    for h, m in ((15, 15), (15, 30), (15, 45), (15, 55), (16, 0)):
        N.FORCE_CLOSE_HOUR, N.FORCE_CLOSE_MINUTE = h, m
        # The replay window has to OUTLAST the force close being tested.
        # _run_arm defaults end=15:45, so the 15:55 and 16:00 arms were
        # bounded by the harness rather than by the setting and could only
        # ever reproduce the 15:45 result. Run every arm to 16:00 and let
        # is_past_force_close do the closing, which is the thing under test.
        trades, per_day = _run_arm(sessions, dtime(9, 45), end=dtime(16, 0))
        label = f"flatten at {h:02d}:{m:02d}" + ("  (current)" if (h, m) == (15, 45) else "")
        _report_daily(label, per_day)
        # ALWAYS report the count, including zero. Five identical rows with no
        # sub-line under them reads as a broken sweep; five identical rows each
        # saying "0 forced exits" reads as the finding it actually is -- at the
        # deployed configuration nothing is still open that late, so the
        # setting cannot matter. The docstring above claims this exit "ends the
        # most trades in the credit window", which was true of an older config
        # and is not true now.
        fc = [t for t in trades if t["reason"] == "FORCE_CLOSE"]
        if fc:
            _report("    the forced exits", fc, len(sessions))
        else:
            print(f"    {'no position reached the force close':32s} "
                  f"0 of {len(trades)} trades")
    N.FORCE_CLOSE_HOUR, N.FORCE_CLOSE_MINUTE = base_h, base_m


def sweep_mstop(sessions: dict):
    """Is the morning stop calibrated for a structure it no longer trades?

    Every one of the eight worst sessions contains a MORNING_DRIFT stop-out,
    and four of them CLOSED UP -- +9.04, +7.07, +6.69, +3.99 -- so the trade
    was stopped on a pullback and the day recovered without it. Losing
    sessions also have a SMALLER average move than winning ones, 4.34 against
    5.73. That is chop, not adverse trend.

    The -20% stop was measured on the DEEP placement, a $322 contract whose
    long leg sat $5 in the money. The window now runs a $260 contract with the
    long leg $2 in the money -- cheaper, and far more sensitive to an ordinary
    pullback, since a shallower long leg carries more gamma. The same
    percentage is a much tighter leash on the structure that replaced it.

    Because the window rides, the stop is the ONLY exit before the ceiling and
    the handoff, so its width is the entire downside decision.
    """
    print("")
    print("MORNING STOP WIDTH -- on the placement actually being traded")
    print("")
    base = PB.WINDOWS
    # -100% is the honest "no stop" arm for a DEBIT spread: the premium paid
    # is the whole risk, so a stop at -100% can never fire before expiry does.
    # It is finite on purpose -- risk sizing divides the loss budget by the
    # stop distance, so an absurd number sizes every contract to zero and
    # returns an empty arm that reads like a result (see sweep_creditstop).
    for stop in (-15.0, -20.0, -30.0, -40.0, -50.0, -70.0, -100.0):
        PB.WINDOWS = tuple(
            replace(w, stop_loss_pct=stop) if w.name == "MORNING_DRIFT" else w for w in base
        )
        PB.ENABLED_WINDOWS = frozenset({"MORNING_DRIFT", "AFTERNOON_CREDIT"})
        trades, per_day = _run_arm(sessions, dtime(9, 45))
        _report_daily(f"stop {stop:+.0f}%" + (" (current)" if stop == -20 else ""), per_day)
        m = [t for t in trades if t["strategy"] == "BULL_CALL_SPREAD"]
        if m:
            _report("    the morning trades", m, len(sessions))
    PB.WINDOWS = base


def sweep_worstdays(sessions: dict):
    """What the losing sessions have in common, if anything.

    Five exit-rule changes have now measured negative and none moved the worst
    day by a cent, which says the tail is not made of trades held too long. So
    look at the tail directly: which sessions, which trades, what the market
    was doing, and whether anything observable BEFORE the entry separates them.
    """
    print("")
    print("ANATOMY OF THE LOSING SESSIONS")
    print("")
    PB.ENABLED_WINDOWS = frozenset({"MORNING_DRIFT", "AFTERNOON_CREDIT"})
    _Account.equity = EQUITY
    rows = []
    for day, bars in sessions.items():
        trades = replay_session(bars, dtime(9, 45), dtime(15, 45))
        session = bars[bars.index.map(lambda t: dtime(9, 30) <= t.time() <= dtime(15, 45))]
        if session.empty:
            continue
        o = float(session["Close"].iloc[0]); c = float(session["Close"].iloc[-1])
        rows.append({"day": day, "pnl": sum(t["pnl"] for t in trades), "trades": trades,
                     "move": c - o, "range": float(session["High"].max()) - float(session["Low"].min())})

    losers = sorted([r for r in rows if r["pnl"] < 0], key=lambda r: r["pnl"])
    winners = [r for r in rows if r["pnl"] > 0]
    print("  worst 8 sessions")
    for r in losers[:8]:
        print("    %s  %+9.2f  move %+6.2f  range %5.2f" % (r["day"], r["pnl"], r["move"], r["range"]))
        for t in r["trades"]:
            print("        %-18s %-12s %2dx  %+8.2f  %s" % (
                t["strategy"], (t["playbook"] or "")[:12], t["qty"], t["pnl"], t["reason"]))

    def avg(g, k):
        return sum(abs(x[k]) for x in g) / len(g) if g else 0.0
    print("")
    print("  losing sessions : n=%d  avg |move| %.2f  avg range %.2f" % (
        len(losers), avg(losers, "move"), avg(losers, "range")))
    print("  winning sessions: n=%d  avg |move| %.2f  avg range %.2f" % (
        len(winners), avg(winners, "move"), avg(winners, "range")))
    reasons_l, reasons_w = {}, {}
    for r in losers:
        for t in r["trades"]:
            reasons_l[t["reason"]] = reasons_l.get(t["reason"], 0) + 1
    for r in winners:
        for t in r["trades"]:
            reasons_w[t["reason"]] = reasons_w.get(t["reason"], 0) + 1
    print("  exits on losing days :", reasons_l)
    print("  exits on winning days:", reasons_w)


def sweep_taper(sessions: dict):
    """Should the giveback shrink as a position approaches its maximum?

    A flat 30% treats a peak of +20% and a peak of +90% identically. The first
    still has most of its decay ahead and wants room; the second has almost
    nothing left to gain and hands back 27 points before booking.
    """
    print("")
    print("GIVEBACK TAPER -- ratchet tightens toward the structure's ceiling")
    print("")
    PB.ENABLED_WINDOWS = frozenset({"MORNING_DRIFT", "AFTERNOON_CREDIT"})
    base_on, base_floor = N.GIVEBACK_TAPER, N.GIVEBACK_TAPER_FLOOR
    for on, floor, label in ((False, 0.0, "flat 30% (current)"),
                             (True, 0.20, "taper to 20% at max"),
                             (True, 0.10, "taper to 10% at max"),
                             (True, 0.05, "taper to 5% at max")):
        N.GIVEBACK_TAPER, N.GIVEBACK_TAPER_FLOOR = on, floor
        trades, per_day = _run_arm(sessions, dtime(9, 45))
        _report_daily(label, per_day)
        reasons = {}
        for t in trades:
            reasons[t["reason"]] = reasons.get(t["reason"], 0) + 1
        print(f"{'':36s} exits {reasons}")
    N.GIVEBACK_TAPER, N.GIVEBACK_TAPER_FLOOR = base_on, base_floor


def sweep_severity(sessions: dict):
    """Does gating the short side on a genuinely weak session rescue it?

    Both morning short structures failed firing on any bearish read. But the
    sample's severe days mostly kept falling -- six of the eight that closed
    $9+ down were still falling after 10:15 -- so the losses plausibly came
    from mild bearish mornings that stalled, not from the ones that meant it.

    Both structures are tested, because on a decline that continues they are
    not equivalent: a bear call spread caps at its credit, roughly $59 a
    contract, while a put debit spread pays up to width-minus-debit, roughly
    $340. If severity is the missing filter, the debit structure should
    benefit far more.
    """
    print("")
    print("SEVERITY GATE -- how far down the session must be before selling into it")
    print("")
    base = PB.WINDOWS
    for drop in (None, 0.25, 0.50, 0.75):
        lab = "any bearish read" if drop is None else f"session down {drop:.2f}%+"
        # (a) bear call spread in the morning
        PB.WINDOWS = tuple(
            replace(w, min_session_drop_pct=drop) if w.name == "MORNING_CREDIT" else w
            for w in base
        )
        PB.ENABLED_WINDOWS = frozenset({"MORNING_DRIFT", "MORNING_CREDIT", "AFTERNOON_CREDIT"})
        trades, per_day = _run_arm(sessions, dtime(9, 45))
        mc = [t for t in trades if (t["playbook"] or "").startswith("MORNING_CREDIT")]
        _report_daily(f"bear CALL, {lab}", per_day)
        if mc:
            _report("    the short trades", mc, len(sessions))

        # (b) put debit spread: the morning window's own bear side
        PB.WINDOWS = tuple(
            replace(w, bullish_only=False, min_session_drop_pct=drop)
            if w.name == "MORNING_DRIFT" else w
            for w in base
        )
        PB.ENABLED_WINDOWS = frozenset({"MORNING_DRIFT", "AFTERNOON_CREDIT"})
        trades, per_day = _run_arm(sessions, dtime(9, 45))
        bp = [t for t in trades if t["strategy"] == "BEAR_PUT_SPREAD"]
        _report_daily(f"put DEBIT, {lab}", per_day)
        if bp:
            _report("    the short trades", bp, len(sessions))
    PB.WINDOWS = base


def sweep_badmorning(sessions: dict):
    """Does selling calls into a falling morning beat sitting it out?

    On a down morning MORNING_DRIFT does not fire -- it is long-only and its
    CLEAN gate will not confirm into a decline -- so nothing trades until
    13:30. The obvious filler is a put debit spread, which measured 27% wins
    at -50.62 a trade. MORNING_CREDIT is the other option: sell calls above a
    market moving away from them, the same structure that works in the
    afternoon, six hours earlier.

    Against it: a call short four dollars out survives 48% of sessions from
    10:15 and 92% from 13:30. The question is whether restricting it to
    bearish sessions closes that gap.
    """
    print("")
    print("BAD MORNINGS -- sitting out against selling calls into the decline")
    print("")
    combos = [
        ("current (drift + credit)", {"MORNING_DRIFT", "AFTERNOON_CREDIT"}),
        ("+ bearish morning credit", {"MORNING_DRIFT", "MORNING_CREDIT", "AFTERNOON_CREDIT"}),
        ("morning credit only", {"MORNING_CREDIT", "AFTERNOON_CREDIT"}),
    ]
    for label, names in combos:
        PB.ENABLED_WINDOWS = frozenset(names)
        trades, per_day = _run_arm(sessions, dtime(9, 45))
        _report_daily(label, per_day)
        mc = [t for t in trades if (t["playbook"] or "").startswith("MORNING_CREDIT")]
        if mc:
            _report("    of which MORNING_CREDIT", mc, len(sessions))


def sweep_latestop(sessions: dict):
    """Should the credit stop tighten as gamma peaks into the close?

    The stop is a share of the credit collected and ignores the clock, so a
    spread sold for 0.60 stops at 1.20 whether that happens at 13:45 or
    15:30. Those are not the same event: a late move against a 0DTE short
    strike travels further, and nothing is left afterwards to recover in.
    """
    print("")
    print("LATE-SESSION CREDIT STOP -- flat -100% against tightening near the close")
    print("")
    PB.ENABLED_WINDOWS = frozenset({"MORNING_DRIFT", "AFTERNOON_CREDIT"})
    base_pct, base_time = N.LATE_STOP_PCT, N.LATE_STOP_TIME
    for pct, when, label in (
        (0.0, dtime(15, 0), "flat -100% (current)"),
        (-60.0, dtime(15, 0), "-60% from 15:00"),
        (-50.0, dtime(15, 0), "-50% from 15:00"),
        (-30.0, dtime(15, 0), "-30% from 15:00"),
        (-50.0, dtime(14, 30), "-50% from 14:30"),
        (-50.0, dtime(15, 15), "-50% from 15:15"),
    ):
        N.LATE_STOP_PCT, N.LATE_STOP_TIME = pct, when
        _, per_day = _run_arm(sessions, dtime(9, 45))
        _report_daily(label, per_day)
    N.LATE_STOP_PCT, N.LATE_STOP_TIME = base_pct, base_time


def sweep_hours(sessions: dict):
    """Where the day's movement actually happens.

    The claim worth testing is that 0DTE profit lives before noon. Half of
    that is a fact about QQQ and needs no engine at all: how much of a
    session's total travel occurs in each hour. The other half is what the
    engine did with it, which is reported by the window that owns each hour.
    """
    print("")
    print("WHEN QQQ MOVES -- share of the session's range and travel, by hour")
    print("")
    hours = [(dtime(9, 30), dtime(10, 30), "09:30-10:30"),
             (dtime(10, 30), dtime(11, 30), "10:30-11:30"),
             (dtime(11, 30), dtime(12, 30), "11:30-12:30"),
             (dtime(12, 30), dtime(13, 30), "12:30-13:30"),
             (dtime(13, 30), dtime(14, 30), "13:30-14:30"),
             (dtime(14, 30), dtime(15, 45), "14:30-15:45")]
    totals = {label: {"range": 0.0, "travel": 0.0, "n": 0} for _, _, label in hours}
    day_range_sum = day_travel_sum = 0.0
    for bars in sessions.values():
        session = bars[bars.index.map(lambda t: dtime(9, 30) <= t.time() <= dtime(15, 45))]
        if len(session) < 20:
            continue
        d_range = float(session["High"].max()) - float(session["Low"].min())
        d_travel = float(session["Close"].diff().abs().sum())
        if d_range <= 0 or d_travel <= 0:
            continue
        day_range_sum += d_range
        day_travel_sum += d_travel
        for start, end, label in hours:
            seg = session[session.index.map(lambda t: start <= t.time() < end)]
            if seg.empty:
                continue
            totals[label]["range"] += float(seg["High"].max()) - float(seg["Low"].min())
            totals[label]["travel"] += float(seg["Close"].diff().abs().sum())
            totals[label]["n"] += 1
    print("  %-13s %10s %10s   %s" % ("hour", "of range", "of travel", "window that owns it"))
    owners = {"09:30-10:30": "(warmup / MORNING_DRIFT from 10:15)",
              "10:30-11:30": "MORNING_DRIFT",
              "11:30-12:30": "morning position rides",
              "12:30-13:30": "rides, hands over 13:25",
              "13:30-14:30": "AFTERNOON_CREDIT",
              "14:30-15:45": "AFTERNOON_CREDIT to 15:00, then force close"}
    for _, _, label in hours:
        t = totals[label]
        if not t["n"]:
            continue
        print("  %-13s %9.1f%% %9.1f%%   %s" % (
            label, t["range"] / day_range_sum * 100, t["travel"] / day_travel_sum * 100,
            owners[label]))
    print("")
    print("  Range shares sum above 100%: each hour's own high-low is counted")
    print("  against the whole session's, and the hours overlap in level.")
    print("  Travel is the sum of absolute bar-to-bar moves and does partition.")


def sweep_weekday(sessions: dict):
    """Does the day of the week change the market we are trading?

    The claim worth testing is specific: if Mondays and Fridays are more
    volatile, a strategy that sells premium should behave differently on them
    -- more credit, but more breaches. Range and survival are facts in the
    bars and need no pricing model; the engine's own P&L is the third column
    and the one that decides anything.
    """
    print("")
    print("BY WEEKDAY -- range and strike survival are model-free; P&L is the engine's")
    print("")
    PB.ENABLED_WINDOWS = frozenset({"MORNING_DRIFT", "AFTERNOON_CREDIT"})
    _, per_day = _run_arm(sessions, dtime(9, 45))
    pnl_by_day = {d["day"]: d for d in per_day}

    names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
    buckets = {n: [] for n in names}
    for day, bars in sessions.items():
        idx = names[day.weekday()] if day.weekday() < 5 else None
        if idx is None:
            continue
        session = bars[bars.index.map(lambda t: dtime(9, 30) <= t.time() <= dtime(16, 0))]
        if session.empty:
            continue
        hi, lo = float(session["High"].max()), float(session["Low"].min())
        open_px = float(session["Close"].iloc[0])
        close_px = float(session["Close"].iloc[-1])
        row = {"range_pct": (hi - lo) / open_px * 100.0,
               "move_pct": abs(close_px - open_px) / open_px * 100.0,
               "pnl": pnl_by_day.get(day, {}).get("pnl", 0.0),
               "trades": pnl_by_day.get(day, {}).get("trades", 0)}
        # Condor survival from 13:30, both wing distances, from the bars alone.
        for off in (3.0, 4.0):
            hits = [i for i, ts in enumerate(bars.index) if ts.time() == dtime(13, 30)]
            if hits:
                i = hits[0]
                spot = float(bars["Close"].iloc[i])
                rest = bars.iloc[i + 1:]
                rest = rest[rest.index.map(lambda t: t.time() <= dtime(15, 45))]
                if not rest.empty:
                    held = (float(rest["High"].max()) < spot + off
                            and float(rest["Low"].min()) > spot - off)
                    row["hold_%d" % int(off)] = held
        buckets[idx].append(row)

    print("  %-10s %3s  %8s %8s   %9s %9s   %9s %7s" % (
        "day", "n", "range%", "|move|%", "cndr $3", "cndr $4", "P&L/day", "green"))
    for name in names:
        g = buckets[name]
        if not g:
            continue
        def hold(key):
            vals = [r[key] for r in g if key in r]
            return ("%3.0f%%" % (sum(vals) / len(vals) * 100)) if vals else "  -"
        avg_pnl = sum(r["pnl"] for r in g) / len(g)
        green = len([r for r in g if r["pnl"] > 0]) / len(g) * 100
        print("  %-10s %3d  %7.2f%% %7.2f%%   %8s %9s   %+9.2f %6.0f%%" % (
            name, len(g),
            sum(r["range_pct"] for r in g) / len(g),
            sum(r["move_pct"] for r in g) / len(g),
            hold("hold_3"), hold("hold_4"), avg_pnl, green))


def sweep_macro(sessions: dict):
    """Do yields, crude and the VIX predict the engine's day?

    The engine fetches all three every cycle and logs them without gating on
    any of them -- deliberately, since nothing had measured whether they
    carry information. This buckets the engine's own daily P&L by each, which
    is the only question that matters: not whether crude moves QQQ, but
    whether knowing crude's move would have changed what we should trade.
    """
    print("")
    print("MACRO INPUTS -- engine P&L per day, bucketed by each input's terciles")
    print("")
    PB.ENABLED_WINDOWS = frozenset({"MORNING_DRIFT", "AFTERNOON_CREDIT"})
    _, per_day = _run_arm(sessions, dtime(9, 45))
    pnl_by_day = {d["day"]: d["pnl"] for d in per_day}

    # Intraday first, because the daily version is lookahead. Bucketing a
    # session by its FULL-DAY yield change scores the engine on information
    # that did not exist when it entered at 10:15, and the effect it shows --
    # falling yields +171.57 a day against rising yields -22.78 -- is mostly
    # a restatement of "QQQ went up today", which is not tradeable in
    # advance. What is observable at the decision is the move so far.
    print("  observable at the decision (session open to the entry bar):")
    for sym, label in (("^TNX", "10Y yield"), ("CL=F", "crude"), ("^VIX", "VIX")):
        try:
            intra = yf.Ticker(sym).history(period="60d", interval="5m")
            if intra.empty:
                print(f"    {label}: no intraday bars")
                continue
            intra = intra.tz_convert(NY)
        except Exception as e:
            print(f"    {label}: intraday fetch failed ({e})")
            continue
        for cutoff, when in ((dtime(10, 15), "by 10:15"), (dtime(13, 30), "by 13:30")):
            rows = []
            for day, pnl in pnl_by_day.items():
                bars = intra[intra.index.map(lambda t: t.date() == day)]
                bars = bars[bars.index.map(lambda t: t.time() <= cutoff)]
                if len(bars) < 3:
                    continue
                first, last = float(bars["Close"].iloc[0]), float(bars["Close"].iloc[-1])
                if first == 0:
                    continue
                rows.append({"chg": (last - first) / first * 100.0, "pnl": pnl})
            if len(rows) < 9:
                continue
            rows.sort(key=lambda r: r["chg"])
            third = len(rows) // 3
            out = []
            for name, g in (("down", rows[:third]), ("flat", rows[third:2 * third]),
                            ("up  ", rows[2 * third:])):
                avg = sum(r["pnl"] for r in g) / len(g)
                green = len([r for r in g if r["pnl"] > 0]) / len(g) * 100
                out.append(f"{name} [{g[0]['chg']:+.2f}..{g[-1]['chg']:+.2f}] {avg:+7.2f}/day {green:3.0f}%")
            print(f"    {label:10s} {when:9s} n={len(rows):2d}  " + "   ".join(out))
    print("")
    print("  full-day change, for contrast -- NOT usable, it contains the future:")

    series = {}
    for sym, label in (("^TNX", "10Y yield"), ("CL=F", "crude"), ("^VIX", "VIX")):
        try:
            hist = yf.Ticker(sym).history(period="6mo", interval="1d")
            if hist.empty:
                continue
            series[label] = hist
        except Exception as e:
            print(f"  {label}: fetch failed ({e})")

    qqq_daily = yf.Ticker("QQQ").history(period="6mo", interval="1d")

    for label, hist in series.items():
        rows = []
        closes = hist["Close"]
        for day, pnl in pnl_by_day.items():
            same = [i for i, ts in enumerate(closes.index) if ts.date() == day]
            if not same or same[0] == 0:
                continue
            i = same[0]
            prev, now = float(closes.iloc[i - 1]), float(closes.iloc[i])
            if prev == 0:
                continue
            rows.append({"day": day, "chg": (now - prev) / prev * 100.0,
                         "level": now, "pnl": pnl})
        if len(rows) < 9:
            print(f"  {label}: only {len(rows)} sessions matched, skipping")
            continue
        for key, kind in (("chg", "change vs prior close"), ("level", "level")):
            rows.sort(key=lambda r: r[key])
            third = len(rows) // 3
            groups = (("low ", rows[:third]), ("mid ", rows[third:2 * third]),
                      ("high", rows[2 * third:]))
            out = []
            for name, g in groups:
                avg = sum(r["pnl"] for r in g) / len(g)
                green = len([r for r in g if r["pnl"] > 0]) / len(g) * 100
                span = f"{g[0][key]:.2f}..{g[-1][key]:.2f}"
                out.append(f"{name} [{span}] {avg:+7.2f}/day {green:3.0f}% green")
            print(f"  {label:10s} {kind:22s} " + "   ".join(out))
    print("")
    print("  For reference, QQQ's own next-day move is the thing all three are")
    print("  supposed to anticipate; the engine's P&L is what it actually needs.")


def sweep_placement(sessions: dict):
    """Morning width and depth, judged on daily P&L with the credit window on.

    The per-trade sweep leaves this genuinely ambiguous -- deep placements
    win more often and lose less on their worst trade, shallow ones average
    more with steadier halves, and the sample is 24 trades. Per-trade is also
    the wrong frame for the decision: the morning window hands its slot to
    the credit window at 13:25, so a placement that stops out early frees the
    slot and one that rides does not.
    """
    print("")
    print("MORNING PLACEMENT -- per day, credit window live")
    print("")
    base = PB.WINDOWS
    for width, depth, label in (
        (5.0, None, "$5 deep (live)"),
        (5.0, 2.0, "$5 long $2 ITM"),
        (6.0, 2.0, "$6 long $2 ITM"),
        (4.0, None, "$4 deep"),
        (6.0, None, "$6 deep"),
    ):
        PB.WINDOWS = tuple(
            replace(w, width=width, long_depth=depth) if w.name == "MORNING_DRIFT" else w
            for w in base
        )
        PB.ENABLED_WINDOWS = frozenset({"MORNING_DRIFT", "AFTERNOON_CREDIT"})
        _, per_day = _run_arm(sessions, dtime(9, 45))
        _report_daily(label, per_day)
    PB.WINDOWS = base


def sweep_condorvs(sessions: dict):
    """Both sides against the one side the engine already sells.

    Same entry bar, same wings, same exits, same model -- so the model's
    overstatement of far-OTM premium is common to both arms and cancels in
    the comparison. What does not cancel is the structural trade: a condor
    collects roughly twice the credit and needs BOTH strikes to hold, and the
    survival table already prices that at 92% against 75% for a four-dollar
    offset at 13:30.
    """
    print("")
    print("CONDOR vs ONE SIDE -- same entry, same wings, same exits, per contract")
    print("")
    for entry in (dtime(13, 30), dtime(14, 30)):
        for offset in (3.0, 4.0, 5.0):
            arms = {"call side only": [], "put side only": [], "condor (both)": []}
            for bars in sessions.values():
                hits = [i for i, ts in enumerate(bars.index) if ts.time() == entry]
                if not hits:
                    continue
                i = hits[0]
                for label, fn in (("call side only", lambda: _run_one_side(bars, i, offset, 3.0, True)),
                                  ("put side only", lambda: _run_one_side(bars, i, offset, 3.0, False)),
                                  ("condor (both)", lambda: _run_condor(bars, i, offset))):
                    r = fn()
                    if r:
                        arms[label].append(r)
            print(f"  {entry.strftime('%H:%M')} entry, ${offset:.0f} out:")
            for label, res in arms.items():
                _report("    " + label, res, len(sessions))


def sweep_trenddepth(sessions: dict):
    """Should a strong trend get a shallower long leg, and a higher ceiling?"""
    print("")
    print("TREND-CONDITIONAL PLACEMENT -- shallow long leg when ADX confirms")
    print("")
    PB.ENABLED_WINDOWS = frozenset({"MORNING_DRIFT", "AFTERNOON_CREDIT"})
    for depth, adx in ((0.0, 0), (2.0, 22), (2.0, 28), (3.0, 22), (3.0, 28)):
        N.TRENDING_LONG_DEPTH, N.TRENDING_ADX_MIN = depth, adx
        _, per_day = _run_arm(sessions, dtime(9, 45))
        label = "deep always (current)" if depth == 0 else f"long ${depth:.0f} ITM when ADX>={adx}"
        _report_daily(label, per_day)
    N.TRENDING_LONG_DEPTH = 0.0


def sweep_daytrend(sessions: dict):
    """How far down a session may be before longs are refused."""
    print("")
    print("SESSION TREND FILTER -- refuse bullish entries below this drop from the open")
    print("")
    PB.ENABLED_WINDOWS = frozenset({"MORNING_DRIFT", "AFTERNOON_CREDIT"})
    for drop in (0.0, 0.25, 0.5, 0.75, 1.0):
        N.DAY_TREND_MAX_DROP_PCT = drop
        _, per_day = _run_arm(sessions, dtime(9, 45))
        _report_daily("off (current)" if drop == 0 else f"refuse longs below -{drop:.2f}%",
                      per_day)
    N.DAY_TREND_MAX_DROP_PCT = 0.0


def sweep_daytype(sessions: dict):
    """What the engine earns on days that go down, sideways and up.

    A backtest average hides regime. If the whole result comes from up days,
    a week of selling tells you nothing until it arrives -- and the question
    of whether anything covers a session that opens bad and keeps going is
    answered by looking at those sessions specifically, not at the mean.

    Buckets are the move from the 09:45 bar to the close, which is the span
    the engine can actually trade.
    """
    print("")
    print("BY DAY TYPE -- 09:45 to close, engine P&L per session")
    print("")
    PB.ENABLED_WINDOWS = frozenset({"MORNING_DRIFT", "AFTERNOON_CREDIT"})
    _Account.equity = EQUITY
    rows = []
    for day, bars in sessions.items():
        opens = [i for i, ts in enumerate(bars.index) if ts.time() >= dtime(9, 45)]
        if not opens:
            continue
        start_px = float(bars["Close"].iloc[opens[0]])
        end_px = float(bars["Close"].iloc[-1])
        move = end_px - start_px
        trades = replay_session(bars, dtime(9, 45), dtime(15, 45))
        rows.append({"move": move, "pct": move / start_px * 100,
                     "pnl": sum(t["pnl"] for t in trades), "trades": trades,
                     "n": len(trades)})

    buckets = [
        ("hard down  (< -0.75%)", lambda r: r["pct"] < -0.75),
        ("down       (-0.75..-0.25)", lambda r: -0.75 <= r["pct"] < -0.25),
        ("flat       (-0.25..+0.25)", lambda r: -0.25 <= r["pct"] <= 0.25),
        ("up         (+0.25..+0.75)", lambda r: 0.25 < r["pct"] <= 0.75),
        ("hard up    (> +0.75%)", lambda r: r["pct"] > 0.75),
    ]
    for label, test in buckets:
        group = [r for r in rows if test(r)]
        if not group:
            print(f"  {label:26s} no sessions")
            continue
        total = sum(r["pnl"] for r in group)
        green = len([r for r in group if r["pnl"] > 0])
        flat_days = len([r for r in group if r["n"] == 0])
        kinds = {}
        for r in group:
            for t in r["trades"]:
                kinds[t["strategy"]] = kinds.get(t["strategy"], 0) + 1
        print(f"  {label:26s} n={len(group):2d}  {total / len(group):+8.2f}/day  "
              f"{total:+9.2f} tot  {green}/{len(group)} green  "
              f"{flat_days} untraded  {kinds}")


def sweep_squeeze(sessions: dict):
    """Does a Bollinger squeeze mark a morning where short strikes survive?

    The claim behind a "neutral day iron condor" is that contracting bands,
    flat moving averages and a mid-range RSI identify a session that will go
    nowhere. That is a testable prediction about price, not about premium, so
    it needs no pricing model: split the sessions by how tight the bands are
    at the entry bar and compare how often each group's short strikes hold.
    """
    print("")
    print("VOLATILITY SQUEEZE -- share of sessions where the strike is never touched")
    print("   split by 20-period band width at the entry bar (tightest third vs rest)")
    print("")
    for entry in (dtime(9, 45), dtime(10, 15)):
        rows = []
        for bars in sessions.values():
            hits = [i for i, ts in enumerate(bars.index) if ts.time() == entry]
            if not hits:
                continue
            i = hits[0]
            close = _seen_at(bars.index[i])["Close"]
            if len(close) < 20:
                continue
            sd = float(close.rolling(20).std().iloc[-1])
            spot = float(close.iloc[-1])
            rest = bars.iloc[i + 1:]
            rest = rest[rest.index.map(lambda t: t.time() <= dtime(15, 45))]
            if rest.empty or sd != sd:
                continue
            rows.append({"sd": sd, "spot": spot,
                         "high": float(rest["High"].max()), "low": float(rest["Low"].min())})
        if not rows:
            continue
        rows.sort(key=lambda r: r["sd"])
        cut = max(len(rows) // 3, 1)
        groups = (("squeezed (tightest 3rd)", rows[:cut]), ("everything else", rows[cut:]))
        print(f"  {entry.strftime('%H:%M')} entry")
        for label, group in groups:
            out = []
            for offset in (3.0, 4.0, 5.0, 6.0):
                held = sum(1 for r in group
                           if r["high"] < r["spot"] + offset and r["low"] > r["spot"] - offset)
                out.append(f"${offset:.0f}: {held / len(group) * 100:3.0f}%")
            band = sum(r["sd"] for r in group) / len(group)
            print(f"    {label:26s} n={len(group):2d}  avg 20-bar sd {band:4.2f}   "
                  + "   ".join(out))


def sweep_bearside(sessions: dict):
    """May the morning window trade its short side?

    It is forbidden today on a measurement taken before this harness existed:
    16 bearish-stack mornings, ITM put spreads at -15.52 a trade, and a sign
    that moved when the judging bar moved. The window has changed since --
    $5 wide, deeper long leg, chain pricing, a different exit ladder -- so
    the prohibition deserves re-testing against the engine that exists now.
    """
    print("")
    print("MORNING SHORT SIDE -- long-only against both directions")
    print("")
    base = PB.WINDOWS
    for bull_only in (True, False):
        PB.WINDOWS = tuple(
            replace(w, bullish_only=bull_only) if w.name == "MORNING_DRIFT" else w
            for w in base
        )
        PB.ENABLED_WINDOWS = frozenset({"MORNING_DRIFT"})
        _Account.equity = EQUITY
        trades, _ = _run_arm(sessions, dtime(10, 15))
        _report("long only (current)" if bull_only else "both directions", trades, len(sessions))
        longs = [t for t in trades if t["strategy"] == "BULL_CALL_SPREAD"]
        shorts = [t for t in trades if t["strategy"] == "BEAR_PUT_SPREAD"]
        if shorts:
            _report("   ...of which long", longs, len(sessions))
            _report("   ...of which short", shorts, len(sessions))
    PB.WINDOWS = base
    _Account.equity = EQUITY


def sweep_ratchetstyle(sessions: dict):
    """A fixed offset from the peak, against a share of it.

    The engine gives back max(peak x share, floor) in RETURN POINTS. A fixed
    dollar offset is the same rule with the share switched off -- $0.25 on a
    $3.30 entry is 7.6 points, and it stays 7.6 points whether the trade has
    made 10% or 200%. That is the whole difference: a share widens as the
    position wins, a fixed offset does not.
    """
    print("")
    print("RATCHET STYLE -- fixed points from peak (a dollar offset) vs share of peak")
    print("")
    PB.ENABLED_WINDOWS = frozenset({"MORNING_DRIFT", "AFTERNOON_CREDIT"})
    base_share, base_floor = N.TRAIL_GIVEBACK, N.MIN_GIVEBACK_PCT
    base_windows = PB.WINDOWS
    for share, floor, label in (
        (0.30, base_floor, "30% of peak (current)"),
        (0.0, 5.0, "fixed 5 points"),
        (0.0, 7.6, "fixed 7.6 pts (= $0.25 on $3.30)"),
        (0.0, 10.0, "fixed 10 points"),
        (0.0, 20.0, "fixed 20 points"),
        (0.0, 30.0, "fixed 30 points"),
    ):
        # The credit window pins its own giveback, so the global alone would
        # change nothing -- which is exactly what the first run of this sweep
        # reported, five identical rows.
        PB.WINDOWS = tuple(
            replace(w, ratchet_giveback=share) if w.name == "AFTERNOON_CREDIT" else w
            for w in base_windows
        )
        N.TRAIL_GIVEBACK, N.MIN_GIVEBACK_PCT = share, floor
        _, per_day = _run_arm(sessions, dtime(9, 45))
        _report_daily(label, per_day)
    N.TRAIL_GIVEBACK, N.MIN_GIVEBACK_PCT = base_share, base_floor
    PB.WINDOWS = base_windows
    _Account.equity = EQUITY


def sweep_cooldown(sessions: dict):
    """How long to refuse the side that just lost, and how big the morning is.

    The cooldown was measured once before, on a different configuration and
    with a different tool. Re-measuring it here is not duplication: the
    engine it governs has changed underneath it -- new widths, chain pricing,
    a different ratchet -- and a stand-down length is only meaningful against
    the trades it is standing down from.
    """
    print("")
    print("POST-LOSS COOLDOWN -- minutes the losing side is refused")
    print("")
    PB.ENABLED_WINDOWS = frozenset({"MORNING_DRIFT", "AFTERNOON_CREDIT"})
    for minutes in (0.0, 15.0, 30.0, 60.0, 120.0):
        _Cooldown.minutes = minutes
        _, per_day = _run_arm(sessions, dtime(9, 45))
        _report_daily(f"{minutes:.0f} min" + (" (current)" if minutes == 30 else ""),
                      per_day)
    _Cooldown.minutes = 30.0
    _Cooldown.win_minutes = float(os.getenv("TRADING_WIN_COOLDOWN_MINUTES", "0"))

    print("")
    print("MORNING SIZE -- capital the debit window may deploy")
    print("")
    base = PB.WINDOWS
    for fraction in (0.04, 0.08, 0.12, 0.20):
        PB.WINDOWS = tuple(
            replace(w, entry_fraction=fraction) if w.name == "MORNING_DRIFT" else w
            for w in base
        )
        PB.ENABLED_WINDOWS = frozenset({"MORNING_DRIFT", "AFTERNOON_CREDIT"})
        _, per_day = _run_arm(sessions, dtime(9, 45))
        _report_daily(f"morning fraction {fraction:.0%}" + (" (current)" if fraction == 0.04 else ""),
                      per_day)
    PB.WINDOWS = base
    _Account.equity = EQUITY


def sweep_daily(sessions: dict):
    """Would trading more per day have earned more per day?

    Every rule tested so far that ADDS trades has cost money: a longer
    morning window took +21.58 a trade to +1.74, a looser credit gate took
    89% wins to 76%, a longer credit window left the total flat. This asks
    the question at the level it was posed -- whole sessions, all windows
    switched on together, one position at a time as the engine actually runs.
    """
    print("")
    print("TRADES PER DAY vs DOLLARS PER DAY -- 60 sessions, one position at a time")
    print("")
    combos = [
        ("credit only", {"AFTERNOON_CREDIT"}),
        ("morning only", {"MORNING_DRIFT"}),
        ("morning + credit (current)", {"MORNING_DRIFT", "AFTERNOON_CREDIT"}),
        ("+ midday grinder", {"MORNING_DRIFT", "ITM_GRINDER", "AFTERNOON_CREDIT"}),
        ("all four windows", {w.name for w in PB.WINDOWS}),
    ]
    for label, names in combos:
        PB.ENABLED_WINDOWS = frozenset(names)
        _, per_day = _run_arm(sessions, dtime(9, 45))
        _report_daily(label, per_day)


def sweep_size(sessions: dict):
    """Same trades, more contracts: where does size stop paying?

    P&L scales linearly with contracts and risk does not, because the daily
    loss cap is a fixed share of equity: past some size one bad session hits
    the cap, entries stop, and the rest of that day's opportunities are
    forfeited. That break is what this looks for, and it is only visible now
    that the harness carries equity and halts like the live engine.
    """
    print("")
    print("SIZE -- credit window's share of the daily risk budget")
    print("")
    base = PB.WINDOWS
    for share in (0.20, 0.35, 0.50, 0.80):
        PB.WINDOWS = tuple(
            replace(w, risk_share=share) if w.name == "AFTERNOON_CREDIT" else w for w in base
        )
        PB.ENABLED_WINDOWS = frozenset({"MORNING_DRIFT", "AFTERNOON_CREDIT"})
        _, per_day = _run_arm(sessions, dtime(9, 45))
        _report_daily(f"credit share {share:.0%}" + (" (current)" if share == 0.20 else ""),
                      per_day)
        print(f"{'':36s} ending equity ${_Account.equity:,.2f}")
    PB.WINDOWS = base

    # Past 35% the risk slice stops binding and the ENTRY FRACTION does --
    # the slice permits contracts the capital allocation cannot buy. So the
    # second half of the size question is how much capital an entry may
    # deploy, held at a slice loose enough not to interfere.
    print("")
    print("SIZE -- capital an entry may deploy, credit slice held at 50%")
    print("")
    base_fraction = N.ENTRY_FRACTION
    PB.WINDOWS = tuple(
        replace(w, risk_share=0.50) if w.name == "AFTERNOON_CREDIT" else w for w in base
    )
    for fraction in (0.10, 0.15, 0.20, 0.30):
        N.ENTRY_FRACTION = fraction
        PB.ENABLED_WINDOWS = frozenset({"MORNING_DRIFT", "AFTERNOON_CREDIT"})
        _, per_day = _run_arm(sessions, dtime(9, 45))
        _report_daily(f"entry fraction {fraction:.0%}" + (" (current)" if fraction == 0.10 else ""),
                      per_day)
        print(f"{'':36s} ending equity ${_Account.equity:,.2f}")
    N.ENTRY_FRACTION = base_fraction
    PB.WINDOWS = base
    _Account.equity = EQUITY


def sweep_creditexit(sessions: dict):
    """Where the credit window's P&L is actually decided.

    Its exits split 47% ratchet, 28% force close, 14% target, 9% stop -- so
    the ratchet's giveback is the single most consequential number in the
    window, and it was inherited from the debit side rather than chosen here.
    Window length is the second: a trade still open at 15:00 keeps running,
    but a NEW one cannot start, and today's closed at 15:19 with the window
    long shut.

    Same structure in every arm, so the model's premium bias cancels.
    """
    print("")
    print("CREDIT RATCHET GIVEBACK -- share of peak handed back before booking")
    print("")
    PB.ENABLED_WINDOWS = frozenset({"AFTERNOON_CREDIT"})
    base_give, base_min = N.TRAIL_GIVEBACK, N.MIN_GIVEBACK_PCT
    base_w = PB.WINDOWS
    for give in (0.15, 0.20, 0.30, 0.50):
        # The window pins its own giveback, so setting the global alone tests
        # nothing -- which is exactly what this sweep reported once the
        # account bug was fixed: four identical rows, to the cent.
        PB.WINDOWS = tuple(
            replace(w, ratchet_giveback=give) if w.name == "AFTERNOON_CREDIT" else w
            for w in base_w
        )
        N.TRAIL_GIVEBACK = give
        trades, _ = _run_arm(sessions, dtime(13, 30))
        _report(f"giveback {give:.0%}" + (" (current)" if give == 0.20 else ""),
                trades, len(sessions))
    N.TRAIL_GIVEBACK = base_give
    PB.WINDOWS = base_w

    print("")
    print("CREDIT WINDOW LENGTH -- how late a NEW credit entry may open")
    print("")
    base_windows = PB.WINDOWS
    for end in (dtime(15, 0), dtime(15, 15), dtime(15, 30)):
        PB.WINDOWS = tuple(
            replace(w, end=end) if w.name == "AFTERNOON_CREDIT" else w for w in base_windows
        )
        PB.ENABLED_WINDOWS = frozenset({"AFTERNOON_CREDIT"})
        trades, _ = _run_arm(sessions, dtime(13, 30))
        _report(f"entries until {end.strftime('%H:%M')}" + (" (current)" if end == dtime(15, 0) else ""),
                trades, len(sessions))
    PB.WINDOWS = base_windows


def sweep_creditgate(sessions: dict):
    """Does making the credit window wait for a directional tier pay for it?

    Both arms trade the same structure at the same width, so the model's
    known overstatement of far-OTM premium is common to them and cancels in
    the comparison -- which is the one thing a model-priced replay can still
    do honestly.
    """
    print("")
    print("CREDIT ENTRY GATE -- directional tier vs trend-only (THETA)")
    print("")
    PB.ENABLED_WINDOWS = frozenset({"AFTERNOON_CREDIT"})
    for loose in (False, True):
        N.CREDIT_LOOSE_GATE = loose
        trades, _ = _run_arm(sessions, dtime(13, 30))
        if trades:
            mins = [t["ts"].hour * 60 + t["ts"].minute for t in trades]
            avg = sum(mins) / len(mins)
            when = f"avg entry {int(avg // 60):02d}:{int(avg % 60):02d}"
        else:
            when = ""
        _report(("trend-only gate" if loose else "directional tier (current)"),
                trades, len(sessions))
        print(f"{'':34s} {when}")
    N.CREDIT_LOOSE_GATE = False


def sweep_rideratchet(sessions: dict):
    """Should a riding position protect a gain, and from what level?

    A ride keeps only its stop and its deadline, which is what let the
    2026-08-21 morning trade peak at +26.4% and close at +16.7%. The counter-
    argument is measured too: booking rides at a fixed target earned +18.82 a
    trade against +36.40 for letting them run. A ratchet is the middle -- it
    only acts after a gain exists -- and the arm level decides whether it
    protects winners or truncates them.
    """
    print("")
    print("RIDE RATCHET -- arm level for a riding position (giveback 20%, floor 5 points)")
    print("")
    PB.ENABLED_WINDOWS = frozenset({"MORNING_DRIFT"})
    for arm in (0.0, 12.0, 18.0, 25.0, 32.0):
        N.RIDE_RATCHET_ARM_PCT = arm
        trades, _ = _run_arm(sessions, dtime(10, 15))
        label = "off (current)" if arm == 0 else f"arm at +{arm:.0f}%"
        _report(label, trades, len(sessions))
    N.RIDE_RATCHET_ARM_PCT = 0.0

    # HIGH ARMS, added 2026-08-28. Every level above was chosen to protect an
    # ordinary winner, and section 10 rejected all of them for truncating the
    # trades that pay for the stop-outs. Arming at +50% is a different rule:
    # it cannot touch a small winner because it never engages on one. Whether
    # that is protection or a sample too thin to read is what this measures.
    print("")
    print("  HIGH ARMS -- only engages on a position that is already a large winner")
    for arm in (40.0, 50.0, 60.0, 75.0):
        N.RIDE_RATCHET_ARM_PCT = arm
        trades, _ = _run_arm(sessions, dtime(10, 15))
        _report(f"arm at +{arm:.0f}%", trades, len(sessions))
    N.RIDE_RATCHET_ARM_PCT = 0.0

    # THE GIVEBACK, AT THE PROPOSED ARM. The arm decides WHETHER the rung
    # engages; the giveback decides how much of the peak it surrenders first.
    # 0.20 of a +50% peak books at +40%, 0.40 books at +30% -- which is the
    # range the question was actually about.
    print("")
    print("  GIVEBACK AT A +50% ARM -- 0.20 books a +50% peak at +40%, 0.40 at +30%")
    base_gb = N.RIDE_GIVEBACK
    N.RIDE_RATCHET_ARM_PCT = 50.0
    for gb in (0.15, 0.20, 0.30, 0.40):
        N.RIDE_GIVEBACK = gb
        trades, _ = _run_arm(sessions, dtime(10, 15))
        _report(f"giveback {gb:.0%} (+50% peak books at +{50 * (1 - gb):.0f}%)",
                trades, len(sessions))
    N.RIDE_GIVEBACK = base_gb
    N.RIDE_RATCHET_ARM_PCT = 0.0

    # PER DAY, BOTH WINDOWS. The morning holds the single position slot, so a
    # rung that ends a ride early also frees that slot early. Per-trade cannot
    # see that; this is the frame the decision belongs in.
    print("")
    print("  PER DAY, BOTH LIVE WINDOWS -- the frame the decision belongs in")
    for arm in (0.0, 32.0, 40.0, 50.0, 60.0):
        N.RIDE_RATCHET_ARM_PCT = arm
        PB.ENABLED_WINDOWS = frozenset({"MORNING_DRIFT", "AFTERNOON_CREDIT"})
        _, per_day = _run_arm(sessions, dtime(10, 15))
        _report_daily("off (current)" if arm == 0 else f"arm at +{arm:.0f}%", per_day)
    N.RIDE_RATCHET_ARM_PCT = 0.0
    PB.ENABLED_WINDOWS = frozenset({"MORNING_DRIFT"})

    # NO RE-ENTRY AFTER A RIDE RATCHET, added 2026-08-28. Every arm above was
    # measured with the slot freed on a ratchet, so each armed row carries the
    # cost of the truncation AND the cost of whatever it re-entered. 36 trades
    # against 31 for off. This isolates the first from the second: same arms,
    # ratchet books and the morning stands down.
    print("")
    print("  RATCHET WITHOUT RE-ENTRY -- books the ride and stands down")
    for arm in (32.0, 40.0, 50.0, 60.0, 75.0):
        N.RIDE_RATCHET_ARM_PCT = arm
        trades, _ = _run_arm(sessions, dtime(10, 15))
        _report(f"arm +{arm:.0f}%, no re-entry", trades, len(sessions))
    print("")
    print("  RATCHET WITHOUT RE-ENTRY -- per day, both live windows")
    for arm in (0.0, 40.0, 50.0, 60.0):
        N.RIDE_RATCHET_ARM_PCT = arm
        PB.ENABLED_WINDOWS = frozenset({"MORNING_DRIFT", "AFTERNOON_CREDIT"})
        _, per_day = _run_arm(sessions, dtime(10, 15))
        _report_daily("off (current)" if arm == 0 else f"arm +{arm:.0f}%, no re-entry",
                      per_day)
    N.RIDE_RATCHET_ARM_PCT = 0.0
    PB.ENABLED_WINDOWS = frozenset({"MORNING_DRIFT"})


def sweep_rideguard(sessions: dict):
    """The two candidate rungs for a riding position, measured side by side.

    2026-08-28 peaked at +59% and booked -18% because a ride has exactly one
    live rule under it. Two mechanisms could put a rung there, and they fail
    differently, so they are measured together rather than one after the other.

    A RATCHET reads a peak and a giveback. It always surrenders part of the
    move by construction, and on a position that prints +46.6/+59.0/+54.3 on
    three consecutive minutes it can fire on noise. Measured here WITHOUT
    re-entry: every earlier ride-ratchet arm freed the slot on a RATCHET exit
    and took 36 trades against 31 for off, so the arm's cost and the
    re-entries' cost were summed and reported as the arm's.

    An ABSOLUTE take-profit fires the first time a level is reached and gives
    nothing back. Section 10 rejected fixed targets on rides -- +18.82 a trade
    against +36.40 for running -- but at the old width, the old -20% stop and
    model pricing, so it is re-measured at the deployed configuration.

    Read the per-DAY block, not the per-trade one. The morning holds the single
    position slot to 13:25, so a rung that ends a ride early also changes what
    the rest of the day can do.
    """
    print("")
    print("RIDE GUARD -- what belongs between the stop and an unreachable ceiling")
    print(f"  pricing: {'CHAIN-CALIBRATED' if CHAIN_PRICING else 'MODEL'}")
    print("")
    base_arm, base_tp = N.RIDE_RATCHET_ARM_PCT, N.RIDE_TAKE_PROFIT_PCT

    def _reset():
        N.RIDE_RATCHET_ARM_PCT, N.RIDE_TAKE_PROFIT_PCT = 0.0, 0.0
        N.RIDE_GIVEBACK_LATE = 0.0

    print("  BASELINE")
    _reset()
    PB.ENABLED_WINDOWS = frozenset({"MORNING_DRIFT"})
    trades, _ = _run_arm(sessions, dtime(10, 15))
    _report("off (current)", trades, len(sessions))

    print("")
    print("  RATCHET -- peak giveback, slot freed on the exit (as the engine runs)")
    for arm in (32.0, 40.0, 50.0, 60.0, 75.0):
        _reset()
        N.RIDE_RATCHET_ARM_PCT = arm
        trades, _ = _run_arm(sessions, dtime(10, 15))
        _report(f"ratchet arm +{arm:.0f}%", trades, len(sessions))

    print("")
    print("  ABSOLUTE TAKE-PROFIT -- books outright the first time it is reached")
    for tp in (40.0, 50.0, 60.0, 75.0, 100.0, 150.0):
        _reset()
        N.RIDE_TAKE_PROFIT_PCT = tp
        trades, _ = _run_arm(sessions, dtime(10, 15))
        _report(f"book at +{tp:.0f}%", trades, len(sessions))

    print("")
    print("  RATCHET, GENUINELY NO RE-ENTRY -- session stands down on a ratchet")
    global STAND_DOWN_AFTER_RIDE_RATCHET
    for arm in (40.0, 50.0, 60.0):
        _reset()
        N.RIDE_RATCHET_ARM_PCT = arm
        STAND_DOWN_AFTER_RIDE_RATCHET = True
        trades, _ = _run_arm(sessions, dtime(10, 15))
        STAND_DOWN_AFTER_RIDE_RATCHET = False
        _report(f"arm +{arm:.0f}%, stands down", trades, len(sessions))

    print("")
    print("  DYNAMIC GIVEBACK -- tightens with the CLOCK, not with the peak")
    base_gb = N.RIDE_GIVEBACK
    for arm in (40.0, 50.0):
        for early, late in ((0.30, 0.05), (0.20, 0.05), (0.30, 0.10)):
            _reset()
            N.RIDE_RATCHET_ARM_PCT = arm
            N.RIDE_GIVEBACK, N.RIDE_GIVEBACK_LATE = early, late
            trades, _ = _run_arm(sessions, dtime(10, 15))
            _report(f"arm +{arm:.0f}%, giveback {early:.0%}->{late:.0%}",
                    trades, len(sessions))
    N.RIDE_GIVEBACK, N.RIDE_GIVEBACK_LATE = base_gb, 0.0

    print("")
    print("  PER DAY, BOTH LIVE WINDOWS -- the frame the decision belongs in")
    _reset()
    PB.ENABLED_WINDOWS = frozenset({"MORNING_DRIFT", "AFTERNOON_CREDIT"})
    _, per_day = _run_arm(sessions, dtime(10, 15))
    _report_daily("off (current)", per_day)
    for arm in (40.0, 50.0, 60.0):
        _reset()
        N.RIDE_RATCHET_ARM_PCT = arm
        PB.ENABLED_WINDOWS = frozenset({"MORNING_DRIFT", "AFTERNOON_CREDIT"})
        _, per_day = _run_arm(sessions, dtime(10, 15))
        _report_daily(f"ratchet +{arm:.0f}%", per_day)
    for arm in (40.0, 50.0):
        _reset()
        N.RIDE_RATCHET_ARM_PCT = arm
        STAND_DOWN_AFTER_RIDE_RATCHET = True
        PB.ENABLED_WINDOWS = frozenset({"MORNING_DRIFT", "AFTERNOON_CREDIT"})
        _, per_day = _run_arm(sessions, dtime(10, 15))
        STAND_DOWN_AFTER_RIDE_RATCHET = False
        _report_daily(f"ratchet +{arm:.0f}%, stands down", per_day)
    for early, late in ((0.30, 0.05), (0.20, 0.05)):
        _reset()
        N.RIDE_RATCHET_ARM_PCT = 40.0
        N.RIDE_GIVEBACK, N.RIDE_GIVEBACK_LATE = early, late
        PB.ENABLED_WINDOWS = frozenset({"MORNING_DRIFT", "AFTERNOON_CREDIT"})
        _, per_day = _run_arm(sessions, dtime(10, 15))
        _report_daily(f"arm +40%, giveback {early:.0%}->{late:.0%}", per_day)
    N.RIDE_GIVEBACK, N.RIDE_GIVEBACK_LATE = 0.20, 0.0
    for tp in (50.0, 75.0, 100.0):
        _reset()
        N.RIDE_TAKE_PROFIT_PCT = tp
        PB.ENABLED_WINDOWS = frozenset({"MORNING_DRIFT", "AFTERNOON_CREDIT"})
        _, per_day = _run_arm(sessions, dtime(10, 15))
        _report_daily(f"book at +{tp:.0f}%", per_day)

    N.RIDE_RATCHET_ARM_PCT, N.RIDE_TAKE_PROFIT_PCT = base_arm, base_tp
    PB.ENABLED_WINDOWS = frozenset({"MORNING_DRIFT"})


def sweep_ridetight(sessions: dict):
    """A ratchet tight enough to behave like a level that follows the trade up.

    Raised 2026-08-28, and it exposes a gap in sweep_rideguard: every ratchet
    arm there used a giveback of 20% or wider, so "ratchet" was only ever
    measured in its loose form. A loose ratchet surrenders a fifth of the peak
    before acting, which is why the fixed +50% take-profit beat it on the day
    that prompted all this -- the level fired at +59.0% while a 20% giveback
    would have waited for +47.2%.

    A TIGHT giveback is a different rule with the same name. At 8% of peak it
    books a +59% ride at +54%, and a +200% ride at +184%: it gives up little
    and stays uncapped, which is the property a fixed level cannot have. That
    is what is measured here.

    RIDE_MIN_GIVEBACK_PCT matters more as the giveback tightens, since the
    rule surrenders max(peak * giveback, floor) and the floor starts binding
    below roughly 0.17 of a 59-point peak. It is swept too rather than left at
    its default, because at these givebacks it is the binding term.
    """
    print("")
    print("RIDE RATCHET, TIGHT -- does a ratchet beat a level when it barely gives back?")
    print(f"  pricing: {'CHAIN-CALIBRATED' if CHAIN_PRICING else 'MODEL'}")
    print("")
    base = (N.RIDE_RATCHET_ARM_PCT, N.RIDE_TAKE_PROFIT_PCT, N.RIDE_GIVEBACK,
            N.RIDE_MIN_GIVEBACK_PCT, N.RIDE_GIVEBACK_LATE)

    def _reset():
        (N.RIDE_RATCHET_ARM_PCT, N.RIDE_TAKE_PROFIT_PCT, N.RIDE_GIVEBACK,
         N.RIDE_MIN_GIVEBACK_PCT, N.RIDE_GIVEBACK_LATE) = 0.0, 0.0, 0.20, 5.0, 0.0

    def _day(label):
        PB.ENABLED_WINDOWS = frozenset({"MORNING_DRIFT", "AFTERNOON_CREDIT"})
        _, per_day = _run_arm(sessions, dtime(10, 15))
        _report_daily(label, per_day)

    print("  REFERENCE")
    _reset(); _day("off (no rung)")
    _reset(); N.RIDE_TAKE_PROFIT_PCT = 50.0; _day("book at +50% (deployed)")

    print("")
    print("  TIGHT RATCHET -- arm x giveback, floor 5 points")
    for arm in (32.0, 40.0, 50.0):
        for gb in (0.05, 0.08, 0.12, 0.15):
            _reset()
            N.RIDE_RATCHET_ARM_PCT, N.RIDE_GIVEBACK = arm, gb
            _day(f"arm +{arm:.0f}%, giveback {gb:.0%}")

    print("")
    print("  THE FLOOR, at the best giveback -- max(peak*gb, floor) points")
    for floor in (2.0, 5.0, 10.0):
        _reset()
        N.RIDE_RATCHET_ARM_PCT, N.RIDE_GIVEBACK, N.RIDE_MIN_GIVEBACK_PCT = 40.0, 0.08, floor
        _day(f"arm +40%, giveback 8%, floor {floor:.0f}pt")

    (N.RIDE_RATCHET_ARM_PCT, N.RIDE_TAKE_PROFIT_PCT, N.RIDE_GIVEBACK,
     N.RIDE_MIN_GIVEBACK_PCT, N.RIDE_GIVEBACK_LATE) = base
    PB.ENABLED_WINDOWS = frozenset({"MORNING_DRIFT"})


def sweep_creditstart(sessions: dict):
    """When may the engine start selling calls -- 11:30, or must it wait to 14:00?

    Asked 2026-08-28, after a session that rose to 723.80 by 11:00, fell to
    715.85 by 13:00, and then offered two cents for a 4-wide call spread when
    the window finally opened at 14:00. The premium was in the morning and the
    window was in the afternoon.

    NOT ALREADY ANSWERED, which is why this exists. MORNING_CREDIT is defined
    10:15-11:30 -- it CLOSES at 11:30, so "sell at 11:30" has never been an
    arm. Section 25 compared 13:30 against 14:00 and nothing earlier. The
    standing 48%-vs-92% strike-survival figure is from sweep breach at a fixed
    $4 offset, not at the deployed 0.25 delta, and it says nothing about what
    the extra premium is worth against the extra risk.

    THE TRADE-OFF THIS MEASURES. Earlier means more time value collected and
    more time for the short strike to be reached. Those pull in opposite
    directions and the net is an empirical question, not an argument.

    Two frames. CREDIT SOLO isolates the start time. BOTH WINDOWS is what
    would actually run, and it matters here more than usual: MORNING_DRIFT
    holds the single position slot until the 13:25 handoff, so a credit window
    opening at 11:30 is frequently locked out by a trade already running.
    """
    print("")
    print("CREDIT WINDOW START -- is the premium in the morning?")
    print(f"  pricing: {'CHAIN-CALIBRATED' if CHAIN_PRICING else 'MODEL'}")
    print(f"  floor {N.MIN_CREDIT:.2f}, short delta {N.CREDIT_SHORT_DELTA:.2f}")
    print("")
    base = PB.WINDOWS
    starts = (dtime(11, 0), dtime(11, 30), dtime(12, 0), dtime(12, 30),
              dtime(13, 0), dtime(13, 30), dtime(14, 0))

    def _with(start):
        return tuple(replace(w, start=start) if w.name == "AFTERNOON_CREDIT" else w
                     for w in base)

    print("  CREDIT WINDOW SOLO -- per trade")
    for st in starts:
        PB.WINDOWS = _with(st)
        PB.ENABLED_WINDOWS = frozenset({"AFTERNOON_CREDIT"})
        trades, _ = _run_arm(sessions, st)
        _report(f"opens {st.strftime('%H:%M')}" + (" (live)" if st == dtime(14, 0) else ""),
                trades, len(sessions))

    print("")
    print("  BOTH LIVE WINDOWS -- per day, what would actually run")
    for st in starts:
        PB.WINDOWS = _with(st)
        PB.ENABLED_WINDOWS = frozenset({"MORNING_DRIFT", "AFTERNOON_CREDIT"})
        _, per_day = _run_arm(sessions, dtime(10, 15))
        _report_daily(f"credit opens {st.strftime('%H:%M')}" +
                      (" (live)" if st == dtime(14, 0) else ""), per_day)
    PB.WINDOWS = base


def sweep_relaxed(sessions: dict):
    """The clock-driven engine against a signal-driven one.

    The architectural objection, raised 2026-08-28: the windows encode WHEN as
    a proxy for WHAT CONDITIONS, a time-of-day rule standing in for a
    market-state rule. Let the signal decide direction and let it trade
    whenever it is valid, with risk controls rather than clock controls.

    It is a fair objection and it has never been tested as a whole. The
    individual relaxations have -- an earlier morning start (section 27), an
    earlier credit start (section 31), a bearish morning (section 10), looser
    morning tiers (the playbook comment) -- and each lost on its own. That is
    not the same as testing the combination, because the argument is that the
    restrictions are individually defensible and collectively too tight.

    Decomposed the way section 24 decomposed the morning package: the full
    relaxation, then each component alone, because a package that loses says
    nothing about which part lost.

    Per DAY with both windows, which is the only frame in which "trade more"
    can be judged -- a rule that adds trades usually adds worse ones.
    """
    print("")
    print("CLOCK-DRIVEN AGAINST SIGNAL-DRIVEN")
    print(f"  pricing: {'CHAIN-CALIBRATED' if CHAIN_PRICING else 'MODEL'}")
    print("")
    base = PB.WINDOWS

    def _run(label, *, tiers=None, both_dirs=False, m_start=None, m_end=None,
             c_start=None):
        ws = []
        for w in base:
            if w.name == "MORNING_DRIFT":
                w = replace(w,
                            entry_tiers=None if tiers == "ALL" else w.entry_tiers,
                            bullish_only=False if both_dirs else w.bullish_only,
                            start=m_start or w.start, end=m_end or w.end)
            elif w.name == "AFTERNOON_CREDIT" and c_start is not None:
                w = replace(w, start=c_start)
            ws.append(w)
        PB.WINDOWS = tuple(ws)
        PB.ENABLED_WINDOWS = frozenset({"MORNING_DRIFT", "AFTERNOON_CREDIT"})
        _, per_day = _run_arm(sessions, dtime(9, 45))
        _report_daily(label, per_day)

    _run("deployed (clock-driven)")
    print("")
    print("  ONE RELAXATION AT A TIME")
    _run("  morning: ALL tiers", tiers="ALL")
    _run("  morning: both directions", both_dirs=True)
    _run("  morning: opens 09:45", m_start=dtime(9, 45))
    _run("  credit: opens 11:00", c_start=dtime(11, 0))
    print("")
    print("  COMBINED")
    _run("  tiers + directions", tiers="ALL", both_dirs=True)
    _run("  FULLY RELAXED (all four)", tiers="ALL", both_dirs=True,
         m_start=dtime(9, 45), m_end=dtime(15, 0), c_start=dtime(11, 0))
    PB.WINDOWS = base


def sweep_regime(sessions: dict):
    """Four day types, and what each rung is worth on each of them.

    Section 33 located the engine's entire drag in nine REVERSAL sessions --
    up at 10:15, closed down -- costing 1,167.60 with no green days, while
    continuous declines earn +9.18 a day and are safe. Every knob in this file
    was chosen on a 60-session average, which pools four populations that
    behave nothing alike.

    So the question a per-day average cannot answer: does the deployed +50%
    rung actually help the bucket that hurts, or does it earn its keep
    somewhere else entirely and leave the reversal days untouched?

    CONT_UP     up at 10:15, closed up      -- trend days, the ride's home
    REVERSAL    up at 10:15, closed down    -- the nine that cost everything
    CONT_DOWN   down at 10:15, closed down  -- the abstention days
    RECOVERY    down at 10:15, closed up    -- the ride's other good regime

    Read the counts before the averages. Nine sessions is a bucket, not a
    sample, and a rung that looks decisive on it may be describing four trades.
    """
    print("")
    print("REGIME SPLIT -- what each rung is worth on each kind of day")
    print(f"  pricing: {'CHAIN-CALIBRATED' if CHAIN_PRICING else 'MODEL'}")
    print("")
    meta = {}
    for day, bars in sessions.items():
        c = bars["Close"].astype(float)
        o, cl = float(c.iloc[0]), float(c.iloc[-1])
        idx = [i for i, ts in enumerate(bars.index) if ts.strftime("%H:%M") >= "10:15"]
        early = (float(c.iloc[idx[0]]) - o) / o * 100 if idx else 0.0
        if early >= 0:
            meta[day] = "CONT_UP" if cl >= o else "REVERSAL"
        else:
            meta[day] = "RECOVERY" if cl >= o else "CONT_DOWN"
    order = ("CONT_UP", "REVERSAL", "CONT_DOWN", "RECOVERY")
    counts = {k: sum(1 for v in meta.values() if v == k) for k in order}
    print("  sessions: " + "  ".join(f"{k} {counts[k]}" for k in order))
    print("")
    base_tp = N.RIDE_TAKE_PROFIT_PCT
    header = "  " + f"{'rung':18s}" + "".join(f"{k + f' ({counts[k]}d)':>18s}" for k in order)
    print(header)
    for tp in (0.0, 50.0, 75.0, 100.0):
        N.RIDE_TAKE_PROFIT_PCT = tp
        PB.ENABLED_WINDOWS = frozenset({"MORNING_DRIFT", "AFTERNOON_CREDIT"})
        _, per_day = _run_arm(sessions, dtime(10, 15))
        tot = {k: 0.0 for k in order}
        for d in per_day:
            tot[meta[d["day"]]] += d["pnl"]
        label = "off" if tp == 0 else f"book at +{tp:.0f}%"
        row = "  " + f"{label:18s}"
        for k in order:
            row += f"{tot[k] / max(counts[k], 1):+13.2f}/day"
        print(row)
    N.RIDE_TAKE_PROFIT_PCT = base_tp


def _zone_buckets(label: str, trades: list, key, order=None):
    """Outcomes bucketed by one zone reading.

    Halves are printed per bucket for the usual reason: a bucket that reverses
    between the first and second half of its own trades is a statement about
    two samples, not about the level.
    """
    buckets = {}
    for t in trades:
        k = key(t)
        if k is None:
            continue
        buckets.setdefault(k, []).append(t)
    if not buckets:
        print(f"  {label}: no trades carried this reading")
        return
    print(f"  {label}")
    keys = order or sorted(buckets)
    for k in keys:
        rows = buckets.get(k)
        if not rows:
            continue
        n = len(rows)
        tot = sum(r["pnl"] for r in rows)
        wins = sum(1 for r in rows if r["pnl"] > 0)
        half = n // 2
        h1 = sum(r["pnl"] for r in rows[:half]) / max(half, 1)
        h2 = sum(r["pnl"] for r in rows[half:]) / max(n - half, 1)
        print(f"    {str(k):22s} {n:3d} tr  {wins / n * 100:3.0f}% win  "
              f"{tot / n:+8.2f}/tr  {tot:+9.2f} tot  halves {h1:+7.2f}/{h2:+7.2f}")


def sweep_zones(sessions: dict):
    """Do fixed price levels predict anything the moving averages miss?

    The proposal is the standard one: mark the day's high and low and the prior
    day's close, and read where price sits against them before entering. The
    engine has never had a fixed level of any kind -- every reading it owns is
    a trailing-window statistic -- so this is a new KIND of input rather than a
    variation on an existing one, which is the only reason it is worth the
    twelfth column.

    Two passes, in this order and not the other:

    1. BUCKETS. Replay the deployed configuration unchanged and sort the trades
       it already takes by the zone reading at entry. Cheap, and it says
       whether there is any signal to gate on before a gate exists.

    2. GATED ARMS. Only meaningful if the buckets separate. Bucketing and
       gating are NOT the same measurement: dropping a trade from a bucket
       leaves the other trades untouched, while refusing an entry frees the
       position slot and a different trade takes it. A bucket that looks awful
       can price out flat once the replacement trades are counted, which is
       why the deployment decision is made on the arms and never on the
       buckets.
    """
    print("")
    print("ZONES -- fixed levels against the day's own range")
    print(f"  pricing: {'CHAIN-CALIBRATED' if CHAIN_PRICING else 'MODEL'}")
    print("")

    PB.ENABLED_WINDOWS = frozenset({"MORNING_DRIFT", "AFTERNOON_CREDIT"})
    trades, per_day = _run_arm(sessions, dtime(10, 15))
    _report_daily("deployed, unchanged", per_day)
    print("")

    longs = [t for t in trades if not is_credit(t["strategy"])]
    print(f"  {len(trades)} trades, {len(longs)} of them debit/long")
    print("")

    def _quartile(t):
        p = t.get("day_range_pos_pct")
        if p is None:
            return None
        if p >= 75:
            return "75-100 (at the high)"
        if p >= 50:
            return "50-75"
        if p >= 25:
            return "25-50"
        return "0-25 (at the low)"

    _zone_buckets("BY POSITION IN THE DAY'S RANGE -- all trades", trades, _quartile,
                  order=["0-25 (at the low)", "25-50", "50-75", "75-100 (at the high)"])
    print("")
    _zone_buckets("BY POSITION IN THE DAY'S RANGE -- debit only", longs, _quartile,
                  order=["0-25 (at the low)", "25-50", "50-75", "75-100 (at the high)"])
    print("")
    _zone_buckets("BY NEAREST LEVEL", trades, lambda t: t.get("zone"))
    print("")
    _zone_buckets("BY PRIOR-DAY RANGE EXTENSION", trades, lambda t: t.get("zone_extension"))
    print("")

    def _gap(t):
        g = t.get("gap_pct")
        if g is None:
            return None
        return "gap up >0.3%" if g > 0.3 else ("gap down <-0.3%" if g < -0.3 else "flat open")

    _zone_buckets("BY OVERNIGHT GAP", trades, _gap,
                  order=["gap down <-0.3%", "flat open", "gap up >0.3%"])
    print("")

    def _prior(t):
        c = t.get("prior_change_pct")
        if c is None:
            return None
        return "up vs prior close" if c > 0 else "down vs prior close"

    _zone_buckets("BY PRIOR-DAY CHANGE", trades, _prior)

    # ---- the arms, which are what a deployment decision reads ----
    print("")
    print("  GATED ARMS -- refusing a long in the top of the day's range")
    print("  (a refused entry frees the slot, so these are NOT the buckets above)")
    base = (N.ZONE_MAX_RANGE_POS_LONG, N.ZONE_MIN_RANGE_POS_SHORT,
            N.ZONE_MIN_RANGE_POS_LONG, N.ZONE_MAX_RANGE_POS_SHORT)

    def _arm(label, max_long=100, min_short=0, min_long=0, max_short=100,
             inside=False, max_gap=0.0):
        N.ZONE_MAX_RANGE_POS_LONG = max_long
        N.ZONE_MIN_RANGE_POS_SHORT = min_short
        N.ZONE_MIN_RANGE_POS_LONG = min_long
        N.ZONE_MAX_RANGE_POS_SHORT = max_short
        N.ZONE_REQUIRE_INSIDE_PRIOR_RANGE = inside
        N.ZONE_MAX_GAP_PCT = max_gap
        PB.ENABLED_WINDOWS = frozenset({"MORNING_DRIFT", "AFTERNOON_CREDIT"})
        _, pd_ = _run_arm(sessions, dtime(10, 15))
        _report_daily(label, pd_)

    _arm("all gates off  (deployed)")
    print("")
    print("  DO NOT BUY THE HIGH -- refusing a long in the top of the range")
    for cap in (90, 80, 70):
        _arm(f"no long above {cap}% of range", max_long=cap)
    print("")
    print("  ONLY BUY STRENGTH -- the opposite rule, refusing a long in the bottom")
    for floor_ in (30, 50, 70):
        _arm(f"no long below {floor_}% of range", min_long=floor_)
    print("")
    print("  THE SHORT SIDE, both directions")
    for floor_ in (20, 30):
        _arm(f"no short below {floor_}% of range", min_short=floor_)
    for cap in (80, 70):
        _arm(f"no short above {cap}% of range", max_short=cap)

    # The two readings that held their sign across both halves as BUCKETS.
    # Bucketing is not gating: these arms are the only thing that prices them,
    # because a refused entry frees the slot for a different trade.
    print("")
    print("  THE TWO THAT SURVIVED THE HALVES -- priced as gates")
    _arm("only inside yesterday's range", inside=True)
    for g in (0.5, 0.3, 0.2):
        _arm(f"no entry after a gap over {g}%", max_gap=g)
    _arm("inside range AND gap under 0.3%", inside=True, max_gap=0.3)

    (N.ZONE_MAX_RANGE_POS_LONG, N.ZONE_MIN_RANGE_POS_SHORT,
     N.ZONE_MIN_RANGE_POS_LONG, N.ZONE_MAX_RANGE_POS_SHORT) = base
    N.ZONE_REQUIRE_INSIDE_PRIOR_RANGE, N.ZONE_MAX_GAP_PCT = False, 0.0


def sweep_bookrenter(sessions: dict):
    """Book the morning ride at a fixed target, pause, then re-enter.

    THREE THINGS CHANGE AT ONCE and the sweep separates them:

      1. A fixed take-profit exists at all. MORNING_DRIFT currently rides --
         only the stall, the ceiling, the stop and the 13:25 handoff end it.
      2. The level of that target.
      3. A pause after a WINNING exit, which did not exist in any form. The
         30-minute cooldown in equity.py fires on losses only, on the stated
         grounds that a winning setup needs no cooling off.

    Section 36 already measured (1) at bar-close pricing and it was expensive:
    +7.62 a day with a +50% take-profit against +113.23 with it off. That
    measurement had no pause and no re-entry, which is exactly the thing being
    proposed here, so it does not settle the question -- it only says the
    take-profit has to EARN back a large deficit from the shots that follow it.

    Run at the live configuration, so intrabar stops and the stall exit are
    both on. That baseline is negative over these sessions (section 44), which
    means every figure here should be read as a RANKING and not as a forecast.
    """
    print("")
    print("BOOK AT A TARGET, PAUSE, RE-ENTER")
    print(f"  pricing: {'CHAIN-CALIBRATED' if CHAIN_PRICING else 'MODEL'}")
    print("")
    base_tp, base_win = N.RIDE_TAKE_PROFIT_PCT, _Cooldown.win_minutes

    def _arm(label, tp, pause):
        N.RIDE_TAKE_PROFIT_PCT = tp
        _Cooldown.win_minutes = pause
        PB.ENABLED_WINDOWS = frozenset({"MORNING_DRIFT", "AFTERNOON_CREDIT"})
        trades, per_day = _run_arm(sessions, dtime(10, 15))
        tps = sum(1 for t in trades if t["reason"] == "TAKE_PROFIT")
        _report_daily(f"{label}  [{len(trades)} tr, {tps} booked at target]", per_day)

    _arm("ride, no target  (deployed)", 0.0, 0.0)
    print("")
    print("  THE ASK: book at +50%, pause 30 min, re-enter")
    _arm("+50% target, 30 min pause", 50.0, 30.0)
    print("")
    print("  IS IT THE TARGET OR THE PAUSE? -- pause length at a +50% target")
    for pause in (0.0, 15.0, 30.0, 45.0, 60.0):
        _arm(f"+50% target, {pause:.0f} min pause", 50.0, pause)
    print("")
    print("  THE TARGET LEVEL, at a 30-minute pause")
    for tp in (30.0, 40.0, 50.0, 60.0, 75.0):
        _arm(f"+{tp:.0f}% target, 30 min pause", tp, 30.0)

    N.RIDE_TAKE_PROFIT_PCT, _Cooldown.win_minutes = base_tp, base_win


def sweep_zonetier(sessions: dict):
    """The ZONE tier: enter off a level that has held, not off the averages.

    Every existing tier is a trend read. This one asks whether the session's
    floor has been TESTED AND HELD -- old low, real lift off it, VWAP agreeing
    -- which is visible while the moving averages are still catching up to the
    reversal that made the floor.

    It can only ADD trades: it sits below CLEAN in the ladder, so a cycle CLEAN
    would have taken is still attributed to CLEAN. That makes the measurement
    unusually clean for this engine -- the question is simply whether the extra
    entries pay, with no confounding from trades that moved between tiers.

    THE MACRO ARM IS THE ONE THAT MATTERS. CLEAN's bullish side requires a GOOD
    verdict, and on 2026-09-02 the verdict was BAD from the open until 10:17,
    which is the entire window in which this tier would have fired early. Keep
    the requirement and the tier mostly cannot act on the mornings it was built
    for; drop it and it is a materially looser engine. Both arms, no assumption.
    """
    print("")
    print("ZONE ENTRY TIER -- a level that held, rather than an average")
    print(f"  pricing: {'CHAIN-CALIBRATED' if CHAIN_PRICING else 'MODEL'}")
    print("")
    base = (N.ZONE_ENTRIES_ENABLED, N.ZONE_HOLD_MINUTES, N.ZONE_BOUNCE_MIN_PCT,
            N.ZONE_BOUNCE_MAX_PCT, N.ZONE_REQUIRE_MACRO)

    def _arm(label, on=True, hold=20.0, lo=0.15, hi=0.60, macro=True):
        N.ZONE_ENTRIES_ENABLED = on
        N.ZONE_HOLD_MINUTES, N.ZONE_BOUNCE_MIN_PCT = hold, lo
        N.ZONE_BOUNCE_MAX_PCT, N.ZONE_REQUIRE_MACRO = hi, macro
        PB.ENABLED_WINDOWS = frozenset({"MORNING_DRIFT", "AFTERNOON_CREDIT"})
        trades, per_day = _run_arm(sessions, dtime(10, 15))
        zt = [t for t in trades if str(t.get("playbook", "")).endswith("ZONE")]
        extra = ""
        if zt:
            tot = sum(t["pnl"] for t in zt)
            extra = f"  [{len(zt)} ZONE tr, {tot:+.0f} from them]"
        _report_daily(label + extra, per_day)

    _arm("ZONE off  (deployed)", on=False)
    print("")
    print("  MACRO STILL REQUIRED, as CLEAN's bullish side requires it")
    for hold in (10.0, 20.0, 30.0):
        _arm(f"ZONE on, low {hold:.0f} min old", hold=hold, macro=True)
    print("")
    print("  MACRO NOT REQUIRED -- the loosening, priced")
    for hold in (10.0, 20.0, 30.0):
        _arm(f"ZONE on, low {hold:.0f} min old, no macro", hold=hold, macro=False)
    print("")
    print("  HOW FAR OFF THE LEVEL IS STILL 'AT' IT (20 min, no macro)")
    for hi in (0.40, 0.60, 1.00, 2.00):
        _arm(f"ZONE on, bounce up to {hi:.2f}%", hold=20.0, hi=hi, macro=False)

    (N.ZONE_ENTRIES_ENABLED, N.ZONE_HOLD_MINUTES, N.ZONE_BOUNCE_MIN_PCT,
     N.ZONE_BOUNCE_MAX_PCT, N.ZONE_REQUIRE_MACRO) = base


def sweep_latestall(sessions: dict):
    """Does the 5-minute quiet timer book too early in the last 45 minutes?

    THE CLAIM, from a live trade on 2026-09-03: a MU 930/950 debit was stalled
    out at 15:17 for -135 and finished 845 higher. The proposed reading is that
    after ~15:15 the tape chops as people close for the day, so a short quiet
    timer reads ordinary noise as a stall and books positions that recover into
    the bell.

    THE COMPETING READING is that late chop is exactly when a peak is most
    likely to be the real one -- there is no session left to recover in -- and
    a wider timer just holds losers longer. Sections 27, 51 and 55 all record
    a plausible late-day pattern that did not survive measurement, so this is
    a test rather than a change.

    Arms retime the stall only AFTER the cutoff; before it, the deployed
    5-minute / 3.3-point rule is untouched in every arm. Runs to 16:00 rather
    than the usual 15:45, since the whole question is about what happens after
    that.

    READ THE LATE-STALL LINE, not the total. Most trades in a session never
    reach the cutoff, so a real effect on the handful that do is diluted to
    invisibility in the per-trade average. If the late count is near zero the
    arms are measuring nothing and the answer is 'no evidence', not 'no effect'.
    """
    global STALL_MINUTES, LATE_STALL_AFTER, LATE_STALL_MINUTES
    print("")
    print("LATE STALL -- a different quiet timer for the last stretch")
    print(f"  pricing: {'CHAIN-CALIBRATED' if CHAIN_PRICING else 'MODEL'}")
    print(f"  base timer {STALL_MINUTES:.0f} min / {STALL_GIVEBACK_PCT:.1f} pts, "
          f"sessions run to 16:00")
    print("")
    base_after, base_late = LATE_STALL_AFTER, LATE_STALL_MINUTES
    # Restored at the end: the per-bar assignment below writes onto the nodes
    # module, and an "all" run would otherwise hand the next sweep whatever
    # the last bar happened to leave there.
    base_ride, base_credit = N.STALL_MINUTES, N.CREDIT_STALL_MINUTES

    def _arm(label, after, minutes):
        global LATE_STALL_AFTER, LATE_STALL_MINUTES
        LATE_STALL_AFTER, LATE_STALL_MINUTES = after, minutes
        trades, per_day = _run_arm(sessions, dtime(9, 45), end=dtime(16, 0))
        # The trades the change can possibly touch: closed at or after the
        # cutoff. Everything else is identical across arms by construction.
        cut = after or dtime(15, 15)
        late = [t for t in trades if t["exit_ts"].time() >= cut]
        ls = [t for t in late if t["reason"] == "STALL"]
        tot = sum(t["pnl"] for t in late)
        extra = (f"  [late {len(late)} tr {tot:+.0f}, "
                 f"of which STALL {len(ls)} {sum(t['pnl'] for t in ls):+.0f}]")
        _report(label + extra, trades, len(sessions))

    _arm("5 min all day  (deployed)", None, 0.0)
    print("")
    print("  WIDER TIMER AFTER 15:15")
    for m in (10.0, 15.0, 20.0):
        _arm(f"{m:.0f} min after 15:15", dtime(15, 15), m)
    _arm("no stall after 15:15", dtime(15, 15), 0.0)
    print("")
    print("  IS THE CUTOFF ITSELF THE RIGHT PLACE? (15 min timer)")
    for hh, mm in ((14, 45), (15, 0), (15, 30)):
        _arm(f"15 min after {hh:02d}:{mm:02d}", dtime(hh, mm), 15.0)

    LATE_STALL_AFTER, LATE_STALL_MINUTES = base_after, base_late
    N.STALL_MINUTES, N.CREDIT_STALL_MINUTES = base_ride, base_credit


def sweep_creditstall(sessions: dict):
    """Let the afternoon credit trade RIDE past its target, as the morning does.

    The stalled-peak rule lives inside the ride branch, so only MORNING_DRIFT
    has ever had it. The credit window books the moment it touches
    final_take_profit_pct, and production sets that to 50 -- the same level
    that is supposed to ARM the trail, collapsing the designed 50-to-90 band to
    nothing. The code comment beside it already records the cost: a spread
    booked at +51% was worth +72% forty-five minutes later.

    With STALL_ON_CREDIT the target arms instead of firing, and the position is
    booked when the profit curve stops climbing.

    Read the STALL count in the exit mix, not just the dollars: if it stays at
    zero the rule is not engaging and the arms are measuring nothing.
    """
    global STALL_MINUTES, STALL_GIVEBACK_PCT
    print("")
    print("CREDIT STALL -- target arms the exit rather than firing it")
    print(f"  pricing: {'CHAIN-CALIBRATED' if CHAIN_PRICING else 'MODEL'}")
    print(f"  LIVE now: STALL_ON_CREDIT={N.STALL_ON_CREDIT}  "
          f"CREDIT_STALL_ARM={N.CREDIT_STALL_REQUIRES_ARM}  "
          f"credit timer {N.CREDIT_STALL_MINUTES:.0f} min / "
          f"{N.CREDIT_STALL_GIVEBACK_PCT:.1f} pts")
    if not N.CREDIT_STALL_REQUIRES_ARM:
        print("  NOTE: arm=false, so every 'arm,' row below books on quiet ALONE, "
              "without waiting for the target.")
    print("")
    base_on, base_m, base_g = N.STALL_ON_CREDIT, STALL_MINUTES, STALL_GIVEBACK_PCT
    base_cm, base_cg = N.CREDIT_STALL_MINUTES, N.CREDIT_STALL_GIVEBACK_PCT

    def _arm(label, on, minutes, giveback):
        global STALL_MINUTES, STALL_GIVEBACK_PCT
        N.STALL_ON_CREDIT = on
        # THE RIDE STAYS AT THE DEPLOYED TIMER IN EVERY ARM.
        #
        # It did not. These arms used to assign STALL_MINUTES, which nodes.py
        # then shared between the credit branch and the morning ride, so every
        # arm retimed BOTH rules and the credit-only totals below still moved
        # with the ride through the account's compounding equity. That is the
        # same confound section 55 records, one layer down: the number was
        # real, the attribution was not.
        #
        # nodes.py now carries CREDIT_STALL_MINUTES / CREDIT_STALL_GIVEBACK_PCT
        # separately, so only they move here.
        N.STALL_MINUTES, N.STALL_GIVEBACK_PCT = base_m, base_g
        STALL_MINUTES, STALL_GIVEBACK_PCT = base_m, base_g
        N.CREDIT_STALL_MINUTES, N.CREDIT_STALL_GIVEBACK_PCT = minutes, giveback
        PB.ENABLED_WINDOWS = frozenset({"MORNING_DRIFT", "AFTERNOON_CREDIT"})
        trades, per_day = _run_arm(sessions, dtime(10, 15))
        cr = [t for t in trades if is_credit(t["strategy"])]
        mix = {}
        for t in cr:
            mix[t["reason"]] = mix.get(t["reason"], 0) + 1
        tot = sum(t["pnl"] for t in cr)
        extra = f"  [{len(cr)} credit tr {tot:+.0f}, {mix}]"
        _report_daily(label + extra, per_day)

    # NOT "(deployed)". Production runs STALL_ON_CREDIT=true with
    # CREDIT_STALL_ARM=false, so the label on this arm was describing a
    # configuration that stopped being live at some point and nobody noticed.
    # The banner above now prints both values, so the labels cannot drift from
    # the droplet again without it being visible in the same output.
    _arm("stall off, books at target", False, 5.0, 5.0)
    print("")
    print("  TARGET ARMS, STALL BOOKS -- how long to wait for a new high")
    for m in (3.0, 5.0, 8.0, 12.0):
        _arm(f"arm, book after {m:.0f} min quiet", True, m, 5.0)
    print("")
    print("  HOW MUCH GIVEBACK COUNTS AS STALLED (5 min quiet)")
    for g in (2.0, 5.0, 10.0, 20.0):
        _arm(f"arm, {g:.0f} pts off the peak", True, 5.0, g)

    N.STALL_ON_CREDIT = base_on
    N.STALL_MINUTES, N.STALL_GIVEBACK_PCT = base_m, base_g
    N.CREDIT_STALL_MINUTES, N.CREDIT_STALL_GIVEBACK_PCT = base_cm, base_cg
    STALL_MINUTES, STALL_GIVEBACK_PCT = base_m, base_g


def sweep_breakeven(sessions: dict):
    """Once a position has shown a profit, never let it go negative.

    The gap, observed live on 2026-09-02: a book peaked at +152.50 and gave
    back to -207.50 with nothing watching. Every profit-protection rule in the
    engine starts at a level that peak never reached -- the stall needs the
    target to arm it, the ratchet needs RATCHET_ARM_PCT, and
    RIDE_RATCHET_ARM_PCT is 0. A trade that makes a small profit and hands it
    all back falls through all of them.

    Measured on the MORNING book, not the credit one. The credit window turns
    8-10 trades in 60 sessions and cannot support a conclusion -- that sample
    is what sank the credit-stall measurement in section 55. The morning book
    has 50-60 and the same gap.

    TWO KNOBS. How much peak profit arms it, and where "flat" is: a stop at
    precisely zero is taken out by the spread on the way past, so the exit
    level is swept separately rather than assumed to be 0.

    Read the BREAKEVEN count in the exit mix. If it stays at zero the rule is
    not engaging and the arms are measuring nothing -- the tell that has cost
    four separate investigations in this file.
    """
    print("")
    print("BREAKEVEN STOP -- a shown profit does not become a loss")
    print(f"  pricing: {'CHAIN-CALIBRATED' if CHAIN_PRICING else 'MODEL'}")
    print("")
    base_a, base_e = N.BREAKEVEN_ARM_PCT, N.BREAKEVEN_EXIT_PCT

    def _arm(label, arm, exit_pct):
        N.BREAKEVEN_ARM_PCT, N.BREAKEVEN_EXIT_PCT = arm, exit_pct
        PB.ENABLED_WINDOWS = frozenset({"MORNING_DRIFT", "AFTERNOON_CREDIT"})
        trades, per_day = _run_arm(sessions, dtime(10, 15))
        mix = {}
        for t in trades:
            mix[t["reason"]] = mix.get(t["reason"], 0) + 1
        _report_daily(label + f"  {mix}", per_day)

    _arm("off  (deployed)", 0.0, 0.0)
    print("")
    print("  HOW MUCH PEAK PROFIT ARMS IT (exit at flat)")
    for arm in (10.0, 15.0, 20.0, 30.0, 50.0):
        _arm(f"arm at +{arm:.0f}%, exit at 0%", arm, 0.0)
    print("")
    print("  WHERE FLAT IS (armed at +20%)")
    for ex in (-5.0, -2.0, 0.0, 2.0, 5.0):
        _arm(f"arm at +20%, exit at {ex:+.0f}%", 20.0, ex)

    N.BREAKEVEN_ARM_PCT, N.BREAKEVEN_EXIT_PCT = base_a, base_e


def sweep_strikeexit(sessions: dict):
    """Leave a short spread BEFORE spot reaches the short strike.

    2026-09-02 is the whole argument. A 20-lot 709 call credit spread went
    -7.50 at 708.55, -397.50 at 708.96, and -657.50 by the time spot crossed
    709 and the breach condition was finally true. Every dollar of the loss
    happened in the 45 cents BEFORE the strike. A rule that fires on the
    crossing is not a control; it is a notification.

    Two mechanisms, swept together because they address the same session from
    different ends:

      CREDIT_STRIKE_EXIT_BUFFER -- exit at a DISTANCE from the short strike,
      in dollars of underlying, on the threatening side.

      CREDIT_STALL_REQUIRES_ARM -- the credit stall currently arms at the
      target. That book peaked at 14.2% of max while the ratchet arms at 32%
      and the stall at 50%, so nothing watched it. With the arm off the stall
      watches from entry, as the morning ride's does.

    Watch the STRIKE_APPROACH count in the exit mix; zero means it is not
    engaging and the arms measure nothing.
    """
    print("")
    print("STRIKE APPROACH -- leave before the strike, not after")
    print(f"  pricing: {'CHAIN-CALIBRATED' if CHAIN_PRICING else 'MODEL'}")
    print("")
    base_b, base_a = N.CREDIT_STRIKE_EXIT_BUFFER, N.CREDIT_STALL_REQUIRES_ARM
    base_s = N.STALL_ON_CREDIT

    def _arm(label, buf, requires_arm=True, stall_on=True):
        N.CREDIT_STRIKE_EXIT_BUFFER = buf
        N.CREDIT_STALL_REQUIRES_ARM = requires_arm
        N.STALL_ON_CREDIT = stall_on
        PB.ENABLED_WINDOWS = frozenset({"MORNING_DRIFT", "AFTERNOON_CREDIT"})
        trades, per_day = _run_arm(sessions, dtime(10, 15))
        cr = [t for t in trades if is_credit(t["strategy"])]
        mix = {}
        for t in cr:
            mix[t["reason"]] = mix.get(t["reason"], 0) + 1
        tot = sum(t["pnl"] for t in cr)
        _report_daily(label + f"  [{len(cr)} credit tr {tot:+.0f}, {mix}]", per_day)

    _arm("deployed (armed stall, no buffer)", 0.0, True, True)
    print("")
    print("  HOW FAR FROM THE SHORT STRIKE TO LEAVE")
    for buf in (0.25, 0.50, 1.00, 2.00):
        _arm(f"exit within ${buf:.2f} of the strike", buf, True, True)
    print("")
    print("  CREDIT STALL WATCHING FROM ENTRY (no arm)")
    _arm("stall from entry, no buffer", 0.0, False, True)
    for buf in (0.50, 1.00):
        _arm(f"stall from entry + ${buf:.2f} buffer", buf, False, True)

    N.CREDIT_STRIKE_EXIT_BUFFER, N.CREDIT_STALL_REQUIRES_ARM = base_b, base_a
    N.STALL_ON_CREDIT = base_s


def sweep_indicators(sessions: dict):
    """Two indicator definitions the engine never examined, from outside notes.

    EMA_CROSS_REFERENCE. ema_cross is one of CLEAN's four conditions and the
    sole blocker on 2-3% of cycles, and three specifications have been in play:
    the code compares the 9 EMA against a SIMPLE 20 average, section 3 of the
    notes describes EMA(20), and outside advice proposes EMA(21). The code
    comment defends the concept and never mentions simple versus exponential,
    so the choice reads as unexamined rather than decided.

    MACD_ZERO_AXIS_GATE. The engine reads only the histogram's sign. The
    outside claim is that the crossover's POSITION matters -- a cross while
    the MACD line is below zero is a turn out of oversold, the same cross far
    above zero is a tiring trend. A genuinely different filter, never tested.

    Per day with both windows, since either change alters which trades exist
    rather than how one is managed.
    """
    print("")
    print("INDICATOR DEFINITIONS -- two the engine never examined")
    print(f"  pricing: {'CHAIN-CALIBRATED' if CHAIN_PRICING else 'MODEL'}")
    print("")
    base_ref, base_gate = N.EMA_CROSS_REFERENCE, N.MACD_ZERO_AXIS_GATE

    def _day(label):
        PB.ENABLED_WINDOWS = frozenset({"MORNING_DRIFT", "AFTERNOON_CREDIT"})
        _, per_day = _run_arm(sessions, dtime(10, 15))
        _report_daily(label, per_day)

    print("  WHAT THE 9 EMA IS COMPARED AGAINST")
    for ref in ("sma20", "ema20", "ema21"):
        N.EMA_CROSS_REFERENCE, N.MACD_ZERO_AXIS_GATE = ref, False
        _day(f"9 EMA vs {ref}" + ("  (deployed)" if ref == "sma20" else ""))

    print("")
    print("  MACD ZERO-AXIS GATE, at the deployed reference")
    for gate in (False, True):
        N.EMA_CROSS_REFERENCE, N.MACD_ZERO_AXIS_GATE = "sma20", gate
        _day("zero-axis gate ON" if gate else "zero-axis gate off  (deployed)")

    print("")
    print("  BOTH, at the best reference")
    N.EMA_CROSS_REFERENCE, N.MACD_ZERO_AXIS_GATE = "ema21", True
    _day("ema21 + zero-axis gate")

    N.EMA_CROSS_REFERENCE, N.MACD_ZERO_AXIS_GATE = base_ref, base_gate


def sweep_afternoon(sessions: dict):
    """What should occupy the afternoon, given that selling premium does not?

    The credit window is worth 0.83 a day -- $49 across three months, 11
    trades in 60 sessions -- while holding the single position slot from
    14:00. Sections 14 and 21 explain why: a credit spread's break-even win
    rate IS its risk ratio and delta IS the market's probability estimate, so
    selling market-priced premium is a fair bet before costs and a losing one
    after. Tuning it has been tried at every width, floor, delta, stop and
    start time in this file.

    The question never asked is why the slot runs a CREDIT structure at all
    when the DEBIT structure beside it earns everything. Four alternatives,
    all with the take-profit off, which section 36 established as the correct
    baseline:

      A  current -- morning to 12:30, handoff 13:25, credit 14:00-15:00
      B  credit window simply off
      C  no handoff: the morning rides to the force close instead
      D  morning entries allowed to 15:00, no handoff, no credit window
      E  the afternoon slot runs the MORNING structure instead of a credit
         one -- same ITM placement, same CLEAN gate, 14:00-15:00

    E is the direct test of the objection. If the afternoon is worth trading
    at all, it should be traded with the structure that works.
    """
    print("")
    print("WHAT BELONGS IN THE AFTERNOON")
    print(f"  pricing: {'CHAIN-CALIBRATED' if CHAIN_PRICING else 'MODEL'}"
          f"  entry fraction {N.ENTRY_FRACTION:.2f}")
    print("")
    base = PB.WINDOWS
    morning = [w for w in base if w.name == "MORNING_DRIFT"][0]

    def run(label, windows, names):
        PB.WINDOWS = windows
        PB.ENABLED_WINDOWS = frozenset(names)
        _, per_day = _run_arm(sessions, dtime(10, 15))
        _report_daily(label, per_day)

    run("A  current (morning + credit)", base, {"MORNING_DRIFT", "AFTERNOON_CREDIT"})
    run("B  credit window OFF", base, {"MORNING_DRIFT"})

    no_handoff = tuple(replace(w, ride_until=None) if w.name == "MORNING_DRIFT" else w
                       for w in base)
    run("C  no handoff, credit OFF", no_handoff, {"MORNING_DRIFT"})

    late = tuple(replace(w, ride_until=None, end=dtime(15, 0))
                 if w.name == "MORNING_DRIFT" else w for w in base)
    run("D  entries to 15:00, no handoff", late, {"MORNING_DRIFT"})

    # E: the afternoon slot, run as a DEBIT window with the morning's shape.
    debit_pm = tuple(
        replace(w, placement=morning.placement, width=morning.width,
                long_depth=morning.long_depth, entry_tiers=morning.entry_tiers,
                bullish_only=morning.bullish_only, bearish_only=False,
                stop_loss_pct=morning.stop_loss_pct,
                take_profit_pct=morning.take_profit_pct,
                ride_to_close=True, ride_until=None,
                entry_fraction=morning.entry_fraction)
        if w.name == "AFTERNOON_CREDIT" else w
        for w in base
    )
    run("E  afternoon runs the MORNING structure", debit_pm,
        {"MORNING_DRIFT", "AFTERNOON_CREDIT"})
    PB.WINDOWS = base


def sweep_straddle(sessions: dict):
    """Long ATM straddles -- the structure class this engine has never had.

    Both outside notes proposed buying volatility around events, and a grep
    for "straddle" or "strangle" across the whole repository returns nothing.
    Every structure here is a vertical, so the engine can express a DIRECTION
    and cannot express "a large move, either way".

    That gap matters because of where the losses live. Section 33 located the
    entire drag in REVERSAL sessions -- up at 10:15, closed down -- which are
    exactly the days a directional debit spread is punished and a straddle is
    not. A structure indifferent to sign is the only untested answer to the
    one bucket that costs money.

    MEASUREMENT ONLY. Nothing here touches the live engine. A straddle is not
    a vertical: the position model carries long_strike/short_strike, the
    broker sends two-leg debit/credit packages, and sizing is premium-at-risk
    rather than width-minus-credit. Building it into a live money engine
    before knowing whether it earns is how sections 22, 24 and 29 happened.

    Priced through chain_pricer -- the same fitted surface the rest of the
    chain-priced sweeps use -- and charged the same slippage and commission a
    vertical pays, since both cross two legs each way.

    READ THE COST FIRST. An ATM straddle buys two at-the-money options, so it
    costs several times a vertical and needs a move to break even, not merely
    a direction. Section 3's daily geometry says the median max move from
    10:15 is about $4.05 and this is what decides it.
    """
    import trading_engine.chain_pricer as CP
    print("")
    print("LONG ATM STRADDLE -- a structure the engine cannot currently express")
    print(f"  pricing: {'CHAIN-CALIBRATED' if CHAIN_PRICING else 'MODEL'}   1 contract per trade")
    print("")

    def price(spot, strike, minutes, vix=None):
        years = max(minutes, 0.0) / (60.0 * 6.5 * 252.0)
        if years <= 0:
            return max(0.0, spot - strike) + max(0.0, strike - spot)
        iv = CP.implied_vol(strike, spot, years, vix)
        return (CP.black_scholes(spot, strike, years, iv, call=True)
                + CP.black_scholes(spot, strike, years, iv, call=False))

    for entry_at in ("09:45", "10:15", "13:30"):
        for tp, sl in ((None, None), (50.0, -40.0), (100.0, -40.0), (50.0, -25.0)):
            trades = []
            for day, bars in sessions.items():
                idx = [i for i, ts in enumerate(bars.index)
                       if ts.strftime("%H:%M") >= entry_at]
                if not idx:
                    continue
                i = idx[0]
                spot0 = float(bars["Close"].iloc[i])
                strike = round_to_strike(spot0)
                m0 = broker_mod.minutes_to_expiry(bars.index[i].to_pydatetime())
                debit = price(spot0, strike, m0)
                if debit <= 0.05:
                    continue
                pnl, reason = None, "FORCE_CLOSE"
                for j in range(i + 1, len(bars)):
                    ts = bars.index[j]
                    if ts.time() > dtime(15, 45):
                        break
                    sp = float(bars["Close"].iloc[j])
                    m = broker_mod.minutes_to_expiry(ts.to_pydatetime())
                    val = price(sp, strike, m)
                    ret = (val - debit) / debit * 100.0
                    pnl = (val - debit)
                    if tp is not None and ret >= tp:
                        reason = "TAKE_PROFIT"; break
                    if sl is not None and ret <= sl:
                        reason = "STOP_LOSS"; break
                if pnl is None:
                    continue
                pnl -= SLIPPAGE_ROUNDTRIP + COMMISSION_ROUNDTRIP
                trades.append({"pnl": round(pnl * 100, 2), "reason": reason,
                               "pct": round(pnl / debit * 100, 2), "debit": debit})
            rule = "hold to 15:45" if tp is None else f"+{tp:.0f}% / {sl:.0f}%"
            if trades:
                avg_debit = sum(t["debit"] for t in trades) / len(trades)
                _report(f"enter {entry_at}, {rule} (avg debit {avg_debit:.2f})",
                        trades, len(sessions))
        print("")


def sweep_stall(sessions: dict):
    """Does "wait for a new high, book when it stops making them" beat riding?

    The proposal: a profit curve hitting a local maximum is not the same as a
    profit curve that is finished. Rather than booking on the first pullback,
    wait -- if it sets another higher high the climb continues, and only when
    it stops making new highs is that the global maximum for the day.

    That is a genuinely different rule from everything tested so far. Section
    39's take-profits fire at a LEVEL regardless of shape. Sections 26 and 30's
    ratchets fire on GIVEBACK, which cannot distinguish a dip inside a climb
    from the end of one. This fires on the ABSENCE OF PROGRESS, which is the
    thing actually being asked about.

    The shape it targets is 2026-08-28: peaked +59.0% at 11:26 and never made
    another high. And the shape it must not damage is the six trades that ran
    past +100% -- every one exited at the 13:25 handoff, so each was still
    climbing when the clock took it.

    Read the trade count as well as the P&L: a stall rule that books early
    also frees the slot, and section 38 established that nothing re-enters.
    """
    print("")
    print("STALLED-PEAK EXIT -- book when the curve stops making new highs")
    print(f"  pricing: {'CHAIN-CALIBRATED' if CHAIN_PRICING else 'MODEL'}")
    print("  NOTE: 5-minute bars, so 'minutes without a new high' has 5-min resolution")
    print("")
    global STALL_MINUTES, STALL_GIVEBACK_PCT
    base_m, base_g = STALL_MINUTES, STALL_GIVEBACK_PCT

    PB.ENABLED_WINDOWS = frozenset({"MORNING_DRIFT", "AFTERNOON_CREDIT"})
    STALL_MINUTES, STALL_GIVEBACK_PCT = 0.0, 0.0
    _, per_day = _run_arm(sessions, dtime(10, 15))
    _report_daily("off (rides to the handoff)", per_day)

    print("")
    for mins in (10.0, 15.0, 25.0):
        for give in (5.0, 10.0, 20.0):
            STALL_MINUTES, STALL_GIVEBACK_PCT = mins, give
            PB.ENABLED_WINDOWS = frozenset({"MORNING_DRIFT", "AFTERNOON_CREDIT"})
            _, per_day = _run_arm(sessions, dtime(10, 15))
            _report_daily(f"no new high for {mins:.0f}min, {give:.0f}pt below peak",
                          per_day)
        print("")
    STALL_MINUTES, STALL_GIVEBACK_PCT = base_m, base_g


def sweep_breach(sessions: dict):
    """How often a short strike survives -- from bars alone, no pricing.

    The one question about credit spreads and condors a replay can still
    answer honestly. Whether the model prices premium correctly is a separate
    argument; whether QQQ travels $4 from 13:30 is a fact in the bars. Combine
    a hold rate here with a real credit from the chain and the expected value
    follows.
    """
    print("")
    print("STRIKE SURVIVAL -- share of sessions where the short strike is never touched")
    print("   one-sided = a call short above spot; condor = either side breached")
    print("")
    for entry in (dtime(9, 45), dtime(10, 15), dtime(13, 30), dtime(14, 30)):
        row = []
        for offset in (2.0, 3.0, 4.0, 5.0, 6.0):
            held_call = held_condor = total = 0
            for bars in sessions.values():
                hits = [i for i, ts in enumerate(bars.index) if ts.time() == entry]
                if not hits:
                    continue
                i = hits[0]
                spot = float(bars["Close"].iloc[i])
                rest = bars.iloc[i + 1:]
                rest = rest[rest.index.map(lambda t: t.time() <= dtime(15, 45))]
                if rest.empty:
                    continue
                total += 1
                up_ok = float(rest["High"].max()) < spot + offset
                down_ok = float(rest["Low"].min()) > spot - offset
                held_call += 1 if up_ok else 0
                held_condor += 1 if (up_ok and down_ok) else 0
            if total:
                row.append(f"${offset:.0f}: {held_call / total * 100:3.0f}% / {held_condor / total * 100:3.0f}%")
        print(f"  {entry.strftime('%H:%M')} entry   " + "   ".join(row))


def sweep_credit(sessions: dict):
    print("\nAFTERNOON_CREDIT -- width, entries 13:30-15:00\n")
    base = PB.WINDOWS
    for width in (1.0, 2.0, 3.0, 4.0, 5.0, 6.0):
        PB.WINDOWS = tuple(
            replace(w, width=width) if w.name == "AFTERNOON_CREDIT" else w for w in base
        )
        PB.ENABLED_WINDOWS = frozenset({"AFTERNOON_CREDIT"})
        trades, _ = _run_arm(sessions, dtime(13, 30))
        _report(f"${width:.0f} wide", trades, len(sessions))
    PB.WINDOWS = base


def _adx(bars: pd.DataFrame, period: int = 14):
    if len(bars) < period * 2:
        return None
    h, l, c = bars["High"], bars["Low"], bars["Close"]
    up, down = h.diff(), -l.diff()
    plus_dm = ((up > down) & (up > 0)) * up
    minus_dm = ((down > up) & (down > 0)) * down
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / period, adjust=False).mean()
    pdi = 100 * plus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr
    mdi = 100 * minus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi)
    return float(dx.ewm(alpha=1 / period, adjust=False).mean().iloc[-1])


def sweep_condor(sessions: dict):
    """The condor, priced the way the engine prices everything else.

    Not run through execution_risk_agent -- the engine has no condor
    structure to execute. This prices the same four legs the shadow log
    marks, entered at a fixed time, exited on a 90% decay target or a 2x
    credit stop, held to the force close otherwise. ADX is a variant because
    it is the gate the structure has been recommended behind.
    """
    print("\nIRON CONDOR -- 3-wide wings, 90% decay target, 2x credit stop\n")
    for entry_time in (dtime(9, 45), dtime(10, 15), dtime(13, 30)):
        for offset in (3.0, 4.0, 5.0):
            for adx_max in (None, 22.0):
                results = []
                for bars in sessions.values():
                    hits = [i for i, ts in enumerate(bars.index) if ts.time() == entry_time]
                    if not hits:
                        continue
                    i = hits[0]
                    if adx_max is not None:
                        adx = _adx(_seen_at(bars.index[i]))
                        if adx is None or adx >= adx_max:
                            continue
                    result = _run_condor(bars, i, offset)
                    if result:
                        results.append(result)
                tag = entry_time.strftime("%H:%M") + f" ${offset:.0f} out" + (
                    f", ADX<{adx_max:.0f}" if adx_max else ", unfiltered")
                _report(tag, results, len(sessions))


def _run_condor(bars: pd.DataFrame, i: int, offset: float, wing: float = 3.0) -> "dict | None":
    spot = float(bars["Close"].iloc[i])
    atm = round_to_strike(spot)
    cs, cl = atm + offset, atm + offset + wing
    ps, pl = atm - offset, atm - offset - wing
    mins = broker_mod.minutes_to_expiry(bars.index[i].to_pydatetime())
    credit = (fill_price(estimate_credit_value(CALL_CREDIT_SPREAD, cs, cl, spot, mins), "sell")
              + fill_price(estimate_credit_value(PUT_CREDIT_SPREAD, ps, pl, spot, mins), "sell"))
    if credit <= 0.02:
        return None

    pnl, reason = None, None
    for j in range(i + 1, len(bars)):
        ts = bars.index[j]
        if ts.time() > dtime(15, 45):
            break
        s = float(bars["Close"].iloc[j])
        m = broker_mod.minutes_to_expiry(ts.to_pydatetime())
        cost = (fill_price(estimate_credit_value(CALL_CREDIT_SPREAD, cs, cl, s, m), "buy")
                + fill_price(estimate_credit_value(PUT_CREDIT_SPREAD, ps, pl, s, m), "buy"))
        pnl, reason = (credit - cost) * 100, "FORCE_CLOSE"
        if cost <= credit * 0.10:
            reason = "TARGET"
            break
        if cost >= credit * 2.0:
            reason = "STOP"
            break
    if pnl is None:
        return None
    return {"pnl": round(pnl, 2), "reason": reason, "pct": round((pnl / 100) / credit * 100, 2)}


def _bs_call(spot: float, strike: float, years: float, iv: float) -> tuple:
    """Black-Scholes call price and delta, zero rate, zero dividend.

    broker.py prices VERTICALS with a CDF on the spread's midpoint, which has
    no single-leg equivalent -- so a single call needs its own pricer. Zero
    rate is a rounding error over three days.
    """
    import math
    if years <= 0:
        intrinsic = max(spot - strike, 0.0)
        return intrinsic, (1.0 if spot > strike else 0.0)
    vt = iv * math.sqrt(years)
    d1 = (math.log(spot / strike) + 0.5 * iv * iv * years) / vt
    d2 = d1 - vt
    nd1 = 0.5 * (1.0 + math.erf(d1 / math.sqrt(2.0)))
    nd2 = 0.5 * (1.0 + math.erf(d2 / math.sqrt(2.0)))
    return spot * nd1 - strike * nd2, nd1


def sweep_orb(sessions: dict, iv: float = 0.185, dte: float = 3.0):
    """Buy a 60-delta weekly call when QQQ breaks the 09:30-09:45 high.

    Nothing here touches the engine -- this is a different instrument (one
    long call, not a vertical) and a different entry (opening-range break,
    not the CLEAN stack), so it is simulated on its own terms and judged
    against what the engine actually earns.

    IV is held constant at the level measured on the live chain for this
    tenor. That is the sim's main weakness: a real breakout often comes with
    a small IV bid, which would help the winners slightly.
    """
    print("")
    print(f"OPENING RANGE BREAK -> 60-delta call, {dte:.0f} DTE, IV {iv:.3f}")
    print("   +20% target / -15% stop, as the note specifies")
    print("")
    for exit_by in (dtime(12, 0), dtime(15, 55)):
        results = []
        for bars in sessions.values():
            opening = [i for i, ts in enumerate(bars.index) if dtime(9, 30) <= ts.time() < dtime(9, 45)]
            if not opening:
                continue
            or_high = float(bars["High"].iloc[opening].max())
            entry_i = None
            for i, ts in enumerate(bars.index):
                if ts.time() < dtime(9, 45) or ts.time() >= exit_by:
                    continue
                if float(bars["Close"].iloc[i]) > or_high:
                    entry_i = i
                    break
            if entry_i is None:
                continue
            spot = float(bars["Close"].iloc[entry_i])
            # The strike whose delta is nearest 0.60, on the listed $1 grid.
            strike = min((round(spot) + k for k in range(-8, 3)),
                         key=lambda K: abs(_bs_call(spot, K, dte / 365.0, iv)[1] - 0.60))
            entry_px, _ = _bs_call(spot, strike, dte / 365.0, iv)
            if entry_px <= 0:
                continue
            entry_ts = bars.index[entry_i]
            pnl_pct, reason = None, None
            for j in range(entry_i + 1, len(bars)):
                ts = bars.index[j]
                if ts.time() >= exit_by:
                    break
                elapsed_days = (ts - entry_ts).total_seconds() / 86400.0
                px, _ = _bs_call(float(bars["Close"].iloc[j]), strike,
                                 max(dte - elapsed_days, 0.0) / 365.0, iv)
                pnl_pct, reason = (px - entry_px) / entry_px * 100.0, "TIME_EXIT"
                if px >= entry_px * 1.20:
                    pnl_pct, reason = 20.0, "TARGET"
                    break
                if px <= entry_px * 0.85:
                    pnl_pct, reason = -15.0, "STOP"
                    break
            if pnl_pct is None:
                continue
            results.append({"pnl": round(pnl_pct / 100.0 * entry_px * 100, 2),
                            "pct": round(pnl_pct, 2), "reason": reason})
        _report(f"exit by {exit_by.strftime('%H:%M')}", results, len(sessions))


def _config_banner() -> None:
    """Print every setting that changes the MAGNITUDE of a result.

    Added 2026-08-29 after a defect that ran silently for a week. sweep.py
    imports the engine, so it inherits the engine's environment defaults --
    and TRADING_ENTRY_FRACTION defaults to 0.10 in nodes.py while the
    deployed engine runs 0.20. Every sweep in sections 27-35 was invoked
    without it, so every dollar figure was about half-scale, and nothing in
    the output said so.

    Rankings survived -- all arms in a run share the same sizing -- but any
    figure used to weigh a cost against a benefit was wrong by a factor of
    two, and one live configuration decision was made on such a figure.

    A harness that silently models a different size from the engine ranks
    correctly and prices wrongly forever. The fix is not to hardcode the
    live value -- that would break the moment the live value changed -- but
    to make the sizing impossible to miss, next to the pricing mode that is
    already declared.
    """
    import trading_engine.nodes as _N
    unset = [v for v in ("TRADING_ENTRY_FRACTION", "TRADING_POSITION_BUDGET")
             if os.getenv(v) is None]
    print("")
    print("  RUN CONFIG -- these set the SCALE of every dollar below")
    print(f"    pricing          {'CHAIN-CALIBRATED' if CHAIN_PRICING else 'MODEL'}"
          f"   tick grid {'on' if TICK_PRICING else 'off'}")
    print(f"    entry fraction   {_N.ENTRY_FRACTION:.2f}"
          f"   equity {EQUITY:,.0f}   contract cap {_N.MAX_CONTRACTS if hasattr(_N, 'MAX_CONTRACTS') else 'n/a'}")
    print(f"    morning stop     {_N.RIDE_TAKE_PROFIT_PCT:+.0f}% take-profit"
          f"   credit floor {_N.MIN_CREDIT:.2f}   short delta {_N.CREDIT_SHORT_DELTA:.2f}")
    # The exit ladder and the morning geometry, for the same reason the sizing
    # is here: they set the magnitude of every figure below and they are the
    # settings most easily left at a default that is not the deployed one.
    _mw = next((w for w in PB.WINDOWS if w.name == "MORNING_DRIFT"), None)
    print(f"    stall exit       {STALL_MINUTES:.0f} min / {STALL_GIVEBACK_PCT:.0f}% giveback"
          f"   intrabar stops {'on' if INTRABAR_STOPS else 'off'}")
    if _mw is not None:
        _sl = "none" if _mw.stop_loss_pct is None else f"{_mw.stop_loss_pct:+.0f}%"
        print(f"    morning window   width {_mw.width:.1f}   stop {_sl}"
              f"   {_mw.start}-{_mw.end}")
    print(f"    windows          {sorted(PB.ENABLED_WINDOWS)}")
    if unset:
        print(f"    !! NOT SET, using code defaults: {', '.join(unset)}")
        print("    !! Dollar figures will not match the deployed engine.")
    print("")


def main():
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    _patch_engine()
    _config_banner()
    sessions = _load_sessions()
    print(f"{len(sessions)} sessions, {min(sessions)} to {max(sessions)}")
    if which in ("morning", "all"):
        sweep_morning(sessions)
    if which in ("windows", "all"):
        sweep_windows(sessions)
    if which in ("daydrop", "all"):
        sweep_daydrop(sessions)
    if which in ("target", "all"):
        sweep_target(sessions)
    if which in ("creditstop", "all"):
        sweep_creditstop(sessions)
    if which in ("trendstop", "all"):
        sweep_trendstop(sessions)
    if which in ("creditcfg", "all"):
        sweep_creditcfg(sessions)
    if which in ("package", "all"):
        sweep_package(sessions)
    if which in ("strength", "all"):
        sweep_strength(sessions)
    if which in ("mincredit", "all"):
        sweep_mincredit(sessions)
    if which in ("handoff", "all"):
        sweep_handoff(sessions)
    if which in ("retries", "all"):
        sweep_retries(sessions)
    if which in ("mstart", "all"):
        sweep_mstart(sessions)
    if which in ("zones", "all"):
        sweep_zones(sessions)
    if which in ("bookrenter", "all"):
        sweep_bookrenter(sessions)
    if which in ("zonetier", "all"):
        sweep_zonetier(sessions)
    if which in ("latestall", "all"):
        sweep_latestall(sessions)
    if which in ("creditstall", "all"):
        sweep_creditstall(sessions)
    if which in ("breakeven", "all"):
        sweep_breakeven(sessions)
    if which in ("strikeexit", "all"):
        sweep_strikeexit(sessions)
    if which in ("events", "all"):
        sweep_events(sessions)
    if which in ("forceclose", "all"):
        sweep_forceclose(sessions)
    if which in ("mstop", "all"):
        sweep_mstop(sessions)
    if which in ("worstdays", "all"):
        sweep_worstdays(sessions)
    if which in ("taper", "all"):
        sweep_taper(sessions)
    if which in ("severity", "all"):
        sweep_severity(sessions)
    if which in ("badmorning", "all"):
        sweep_badmorning(sessions)
    if which in ("latestop", "all"):
        sweep_latestop(sessions)
    if which in ("hours", "all"):
        sweep_hours(sessions)
    if which in ("weekday", "all"):
        sweep_weekday(sessions)
    if which in ("macro", "all"):
        sweep_macro(sessions)
    if which in ("placement", "all"):
        sweep_placement(sessions)
    if which in ("condorvs", "all"):
        sweep_condorvs(sessions)
    if which in ("trenddepth", "all"):
        sweep_trenddepth(sessions)
    if which in ("daytrend", "all"):
        sweep_daytrend(sessions)
    if which in ("daytype", "all"):
        sweep_daytype(sessions)
    if which in ("squeeze", "all"):
        sweep_squeeze(sessions)
    if which in ("bearside", "all"):
        sweep_bearside(sessions)
    if which in ("ratchetstyle", "all"):
        sweep_ratchetstyle(sessions)
    if which in ("cooldown", "all"):
        sweep_cooldown(sessions)
    if which in ("size", "all"):
        sweep_size(sessions)
    if which in ("daily", "all"):
        sweep_daily(sessions)
    if which in ("creditexit", "all"):
        sweep_creditexit(sessions)
    if which in ("creditgate", "all"):
        sweep_creditgate(sessions)
    if which in ("rideratchet", "all"):
        sweep_rideratchet(sessions)
    if which in ("rideguard", "all"):
        sweep_rideguard(sessions)
    if which in ("ridetight", "all"):
        sweep_ridetight(sessions)
    if which in ("creditstart", "all"):
        sweep_creditstart(sessions)
    if which in ("relaxed", "all"):
        sweep_relaxed(sessions)
    if which in ("regime", "all"):
        sweep_regime(sessions)
    if which in ("indicators", "all"):
        sweep_indicators(sessions)
    if which in ("afternoon", "all"):
        sweep_afternoon(sessions)
    if which in ("straddle", "all"):
        sweep_straddle(sessions)
    if which in ("stall", "all"):
        sweep_stall(sessions)
    if which in ("breach", "all"):
        sweep_breach(sessions)
    if which in ("credit", "all"):
        sweep_credit(sessions)
    if which in ("condor", "all"):
        sweep_condor(sessions)
    if which in ("orb", "all"):
        sweep_orb(sessions)


if __name__ == "__main__":
    main()
