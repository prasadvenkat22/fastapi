"""The trading graph's two agents.

market_signals_agent reads the macro tape (breadth, VIX, yields, crude, and the
stored headline read) into a verdict and a halt flag. execution_risk_agent is
the deterministic rule engine: it manages the open position and, when flat,
enters on a 1-minute Bollinger band touch (section 260) -- the engine's only
entry since the old tier ladder and its windows were retired (section 261).
"""

import logging
import os
import time
from datetime import datetime, timezone
from typing import List
from zoneinfo import ZoneInfo

from GENAI.vector_stores import VoyageEmbeddings

from . import tradier_orders
from .breadth_history import RECENT_WINDOW_MINUTES, record_and_summarize
from .equity import (MAX_CONSECUTIVE_LOSSES, blocked_direction, consecutive_losses_today,
                     current_equity, entry_cap_reached, win_pause_active)
from . import playbook as PB
from . import exit_rules as XR
from .playbook import strikes_for, thresholds_for, window_for, window_for_direction
from .broker import (
    BEAR_PUT_SPREAD,
    BULL_CALL_SPREAD,
    MockBrokerClient,
    default_mock_broker,
    is_credit,
    option_type_for,
    estimate_spread_value,
    fill_price,
    round_to_strike,
)
NY = ZoneInfo("America/New_York")

from .data_feed import (fetch_market_breadth, fetch_qqq_bars, fetch_oil, fetch_qqq_spot,
                        fetch_tnx, fetch_vix, chain_vertical, fetch_option_chain,
                        log_price_divergence)
from .state import TradingState

logger = logging.getLogger(__name__)

KILL_SWITCH_PATH = "KILL_SWITCH.txt"

# Debit-spread strategy config — see execution_risk_agent below.
POSITION_BUDGET = float(os.getenv("TRADING_POSITION_BUDGET", "1000"))
MIN_ONE_CONTRACT = os.getenv("TRADING_MIN_ONE_CONTRACT", "false").lower() == "true"   # section 249


def cap_to_buying_power(quantity: int, per_contract: float) -> int:
    """Contracts the account can actually pay for (section 239); 0 if unreadable.

    per_contract is what one contract ties up at the broker -- the debit on a
    debit spread, width minus credit on a credit one -- the same figure
    tradier_orders.opening_requirement checks, so an entry sized here is never
    refused there. Only while orders are live: paper runs and sweeps size as
    before.
    """
    if not tradier_orders.LIVE_ORDERS or quantity <= 0 or per_contract <= 0:
        return quantity
    bp = tradier_orders.buying_power()
    if bp is None:
        logger.warning("Buying power unreadable — no entry this cycle.")
        return 0
    fit = int(bp // per_contract)
    if fit < quantity:
        logger.info(
            "Buying power: $%.2f pays for %d contract(s) at $%.0f each — sizing %d, not %d.",
            bp, fit, per_contract, fit, quantity)
        return fit
    return quantity

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

# Opening warmup. Entries wait this many minutes after the bell so the
# opening auction's whipsaws don't get read as a trend; position management
# is unaffected and runs from the first cycle.
MARKET_OPEN_HOUR, MARKET_OPEN_MINUTE = 9, 30
WARMUP_MINUTES = int(os.getenv("TRADING_WARMUP_MINUTES", "15"))

# Hard flatten time. 15:45 leaves half an hour of contract life (expiry is
# 16:15, see broker.EXPIRY_HOUR). TRADING_FORCE_CLOSE_TIME wins when set; else
# the settings page's single force-close row, TRADING_ORPHAN_FORCE_CLOSE, so
# the engine and the exit ladder flatten at the same minute (section 261).
_force_close_raw = (os.getenv("TRADING_FORCE_CLOSE_TIME")
                    or os.getenv("TRADING_ORPHAN_FORCE_CLOSE") or "15:45")
try:
    FORCE_CLOSE_HOUR, FORCE_CLOSE_MINUTE = (int(p) for p in _force_close_raw.split(":"))
except ValueError:
    FORCE_CLOSE_HOUR, FORCE_CLOSE_MINUTE = 15, 45


BAND_TOUCH_PERIOD = int(float(os.getenv("TRADING_BAND_TOUCH_PERIOD", "20") or 20))
BAND_TOUCH_SD = float(os.getenv("TRADING_BAND_TOUCH_SD", "2.0") or 2.0)
# TREND CHECK (section 268). On 10-05 QQQ trended up all day (751 -> 755):
# upper-band puts lost -26 and -24 as price rode the band, lower-band calls
# won. On: no put while the 1-minute 20-SMA has risen more than
# TREND_MIN dollars over the last TREND_BARS minutes, no call while it has
# fallen that much. Off by default.
BAND_TOUCH_TREND_CHECK = os.getenv("TRADING_BAND_TOUCH_TREND_CHECK", "false").lower() == "true"
BAND_TOUCH_TREND_BARS = int(float(os.getenv("TRADING_BAND_TOUCH_TREND_BARS", "15") or 15))
BAND_TOUCH_TREND_MIN = float(os.getenv("TRADING_BAND_TOUCH_TREND_MIN", "0.25") or 0.25)
# MACRO GATE and MACD CHECK (section 269, operator's checklist). Calls need the
# 10Y not up more than MACRO_YIELD_BPS vs the open, crude not up more than
# MACRO_OIL_PCT, and the QQQ macro news not bearish; puts the mirror (not
# falling that much, news not bullish). MACD: the 1-minute histogram (12/26/9)
# rising for a call (the drop is losing speed), falling for a put. Both off.
BAND_TOUCH_MACRO_GATE = os.getenv("TRADING_BAND_TOUCH_MACRO_GATE", "false").lower() == "true"
BAND_TOUCH_MACRO_YIELD_BPS = float(os.getenv("TRADING_BAND_TOUCH_MACRO_YIELD_BPS", "2") or 2)
BAND_TOUCH_MACRO_OIL_PCT = float(os.getenv("TRADING_BAND_TOUCH_MACRO_OIL_PCT", "0.5") or 0.5)
BAND_TOUCH_MACD_CHECK = os.getenv("TRADING_BAND_TOUCH_MACD_CHECK", "false").lower() == "true"
_NEWS_BEARISH = {"BEARISH", "VERY_BEARISH"}
_NEWS_BULLISH = {"BULLISH", "VERY_BULLISH"}
_news_cache: dict = {}
# The 20-SMA exit only books once the spread is up at least this much (section
# 266). 10-05 12:22 ET: QQQ reached the 20-SMA in a $0.48-wide band and the
# exit sold a 751/753 call spread at +0.3%, $0. Below the floor the position
# keeps its take profit, stop and force close. 0 = the old behaviour.
BAND_TOUCH_MIN_PROFIT_PCT = float(os.getenv("TRADING_BAND_TOUCH_MIN_PROFIT_PCT", "0") or 0)


def _band_touch() -> "dict | None":
    """The 1-minute band and the live price, for TRADING_BAND_TOUCH.

    Band: BAND_TOUCH_PERIOD closed 1-minute bars, mean +/- BAND_TOUCH_SD sample
    standard deviations (the same construction as the 5-minute band). Price:
    a live Tradier quote, falling back to the newest 1-minute bar. Returns
    {spot, mid, upper, lower, touch} with touch 'LOWER', 'UPPER' or None, or
    None when the data is missing -- which means no entry and no band exit.
    """
    try:
        bars = fetch_qqq_bars(period="1d", interval="1m")
        close = bars["Close"].astype(float)
        if len(close) < BAND_TOUCH_PERIOD:
            return None
        last = close.iloc[-BAND_TOUCH_PERIOD:]
        mid, sd = float(last.mean()), float(last.std())
        spot = None
        try:
            from .data_feed import _tradier_quote
            q = _tradier_quote("QQQ") or {}
            spot = float(q.get("last") or 0) or None
        except Exception:
            spot = None
        spot = spot or float(fetch_qqq_spot())
        upper, lower = mid + BAND_TOUCH_SD * sd, mid - BAND_TOUCH_SD * sd
        touch = "LOWER" if spot <= lower else ("UPPER" if spot >= upper else None)
        slope = None
        n = BAND_TOUCH_PERIOD + BAND_TOUCH_TREND_BARS
        if BAND_TOUCH_TREND_BARS > 0 and len(close) >= n:
            then = float(close.iloc[-n:-BAND_TOUCH_TREND_BARS].mean())
            slope = mid - then                    # $ change of the 20-SMA
        hist = hist_prev = None
        if len(close) >= 35:
            macd = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
            h = macd - macd.ewm(span=9, adjust=False).mean()
            hist, hist_prev = float(h.iloc[-1]), float(h.iloc[-2])
        return {"spot": spot, "mid": mid, "upper": upper, "lower": lower, "touch": touch,
                "slope": slope, "macd_hist": hist, "macd_hist_prev": hist_prev}
    except Exception:
        logger.exception("Band touch: could not read the 1-minute band.")
        return None


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

# THE SAME CONFIRMATION THE ORPHAN STOP GOT, ON THE PATH THE ENGINE USES FOR
# ITS OWN POSITIONS.
#
# 2026-09-15 fixed the orphan stop: it fired on one print and closed a QQQ
# spread that was +31% seven minutes later. That fix landed in orphans.py,
# which manages positions the engine did NOT open. Positions the engine opens
# itself exit through THIS file, and these stops had no confirmation at all --
# one cycle below the level and they sell.
#
# THE GAP WAS INVISIBLE because that whole session's positions were manual, so
# every stop in the log came from the orphan path and this one was never
# exercised. With MORNING_PUT and ITM_GRINDER enabled the engine opens its own
# again, and would have used the unfixed one.
#
# WORSE ON A RISK-OFF DAY: RISK_OFF_STOP_LOSS_PCT is -13%, tighter still, and
# fires whenever macro reads BAD -- 482 of that session's cycles.
STOP_CONFIRM_MINUTES = float(os.getenv("TRADING_STOP_CONFIRM_MINUTES", "5"))

# See the headline-read block in the indicator node. Cooldown, not a kill, so
# a raised quota recovers without a restart.
EMBED_COOLDOWN_S = float(os.getenv("TRADING_EMBED_COOLDOWN_S", "1800"))
_EMBED_DEAD_UNTIL = 0.0


def _stop_clock_key(position) -> str:
    return f"{position.underlying}|{position.long_strike:g}/{position.short_strike:g}|{position.opened_at}"


def _stop_confirmed(position, return_pct: float, stop_pct: float) -> bool:
    """Tick the stop clock; True when it has held for STOP_CONFIRM_MINUTES.

    Called on EVERY cycle with a position open, past the stop or not, so the
    clock sees recoveries (in a row: reset; total: paused). The clock lives in
    a file (exit_rules.ENGINE_CLOCK_PATH) because each cron cycle is a new
    process -- section 271. 0 disables: the first reading past the stop sells.
    """
    breaching = return_pct <= stop_pct
    if STOP_CONFIRM_MINUTES <= 0:
        return breaching
    key = _stop_clock_key(position)
    rec = XR.load_engine_clock(key)
    total = XR.stop_confirm_total()
    held = XR.stop_clock(rec, breaching, datetime.now(timezone.utc), total)
    XR.save_engine_clock(key, rec)
    if not breaching:
        return False
    # 10 s of slack, as in orphans.py: cycles land 55-65 s apart.
    if held >= STOP_CONFIRM_MINUTES - 10.0 / 60.0:
        return True
    logger.info(
        "%s is %+.1f%%, past the %+.1f%% stop, but only for %.1f of the %.0f "
        "minutes needed to confirm (%s) — holding.",
        position.underlying, return_pct, stop_pct, held, STOP_CONFIRM_MINUTES,
        "total minutes" if total else "in a row",
    )
    return False

# Macro risk-off thresholds. VIX is judged on both level and session move:
# a spike of this magnitude is treated as risk-off even from a low base.
VIX_LEVEL_MAX = float(os.getenv("TRADING_VIX_LEVEL_MAX", "22.0"))
VIX_SPIKE_PCT = float(os.getenv("TRADING_VIX_SPIKE_PCT", "10.0"))

# Crude's intraday move, in percent from the session open, that forces
# risk-off. 0 disables the term entirely, which is what it was until
# 2026-09-06 -- "fetched and REPORTED but does not gate anything yet".
# See the block beside the gates dict for the measurement this was added
# against, and section 112.
CRUDE_SPIKE_PCT = float(os.getenv("TRADING_CRUDE_SPIKE_PCT", "0"))

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
#
# IT HAS NEVER FIRED ON A TRADE. Re-tested 2026-08-25 against intraday ^TNX
# over 49 sessions and the 31 CLEAN morning entries inside them:
#
#     no yield gate          31 tr  35% win   -2.31/tr
#     skip when TNX >= +2bp  28 tr  36% win   -7.34/tr   (blocks 3)
#     skip when TNX >= +3bp  31 tr  35% win   -2.31/tr   (blocks 0)
#     skip when TNX >= +4bp  31 tr  35% win   -2.31/tr   (blocks 0)  <- live
#     skip when TNX >= +6bp  31 tr  35% win   -2.31/tr   (blocks 0)
#
# So the three sessions that reached 4bp never coincided with an entry, and
# the threshold is inert rather than protective. The one level that DOES
# block (2bp) makes the result worse, so the three trades it rejects were
# among the better ones.
#
# Left at 4.0 because inert costs nothing and the tail case it is aimed at --
# a genuine rate shock -- is not in a 49-session sample. Do NOT tighten it to
# 2bp on the strength of the paragraph above this one; that was measured and
# it loses.
#
# Crude was tested the same way and is NOT gated for the same reason, only
# worse: every threshold that blocks anything makes the result worse, and at
# +0.5% the ten entries it would reject average +0.15 a trade against -3.48
# for the twenty-six it keeps. It filters out the better half.
TNX_SPIKE_BPS = float(os.getenv("TRADING_TNX_SPIKE_BPS", "4.0"))

# Breadth is judged on level and trend alike. Expressed as a drop in net
# breadth ratio (advancers-minus-decliners over basket size) from its peak
# over the recent window: 0.40 is roughly a fifth of the basket flipping
# from advancing to declining inside half an hour — participation draining
# out of a tape that still prints positive.
BREADTH_COLLAPSE_RATIO = float(os.getenv("TRADING_BREADTH_COLLAPSE_RATIO", "0.40"))

# Whether the LLM verdict is one of the AND-terms in the macro gate.
#
# The other four terms are arithmetic on measured numbers -- breadth, VIX
# level, VIX velocity, yield velocity. This one is a language model reading
# headlines, and it is the term that has been binding:
#
#     2026-08-24   55 cycles passed all four objective terms,  0 went GOOD
#     2026-08-25   31 cycles passed all four objective terms, 11 went GOOD
#
# It is NOT uniformly negative -- 08-19 ran 67% GOOD and 08-21 74% -- so
# deleting it would be trading one unmeasured gate for another. And it cannot
# be swept: scripts/sweep.py's _session_state hardcodes market_sentiment to
# GOOD, so every parameter in this engine was measured with this gate wired
# open. Its contribution, in either direction, is unknown.
#
# SWITCHED OFF LIVE on 2026-08-25 (TRADING_MACRO_LLM_GATE=false in the
# droplet's .env.production). The default here stays "true" so a fresh
# checkout behaves as it always did; the deployed engine is the experiment.
#
# The argument for turning it off is not that it is bad -- it is that nobody
# knows. Every number in strategy_notes.txt was produced with this term wired
# open, so the deployed engine was strictly more restrictive than anything
# ever measured, by an unmeasured amount. Off, the two are the same engine.
#
# The four objective terms remain: breadth positive and not collapsing, VIX
# under 22, VIX velocity under 10%, yields under 4bp. What is given up is the
# headline read -- it refused on a Putin nuclear-doctrine story on 2026-08-25
# -- and that may well be worth something. macro_block_reason now records
# every refusal, so a few weeks of forward data answers it either way.
MACRO_LLM_GATE = os.getenv("TRADING_MACRO_LLM_GATE", "true").lower() == "true"

# The macro verdict had a model behind it until 2026-09-13. It does not now:
# the Claude call was removed at the operator's instruction and no grader has
# replaced it. The four objective terms -- breadth level and trend, VIX level
# and velocity, yield velocity -- are what the macro read consists of today.
#
# TRADING_MACRO_LLM_GATE stays in the code because the wiring is still there
# and a FinBERT read of the macro RSS tape would slot straight into it. With
# no grader configured the verdict is NOT_GRADED, which is inert while the
# gate is off and refusing while it is on.

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
    from sqlalchemy import text as sqltext
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
            # HISTORY, not just the cache. MacroCache is ONE ROW, upserted --
            # every verdict the model has ever given overwrote the last, so
            # after weeks of running there was nothing to analyse and no way
            # to ask whether the macro read predicted anything. That is what
            # made the 5-minute cadence pure cost: the call was paid for and
            # the answer discarded. An append here is what turns the spend
            # into a dataset.
            db.execute(sqltext(
                "INSERT INTO trading_macro_verdicts "
                "(verdict, confidence, risk_factor, recorded_at) "
                "VALUES (:v, :c, :r, now())"),
                {"v": verdict, "c": confidence, "r": risk_factor})
            db.commit()
        finally:
            db.close()
    except Exception:
        # Non-fatal: a failed write just means the next cycle recomputes.
        logger.exception("Macro cache write failed.")

# Headline -> feed it arrived on, for the scrape just completed. A module
# global rather than a return value because _scrape_headlines() is called from
# one place and store_headlines() from another, and threading a second value
# through market_signals_agent would touch the cycle's hot path for a column
# that is observational.
_LAST_SOURCES: dict = {}

# Headline -> its real publication time, from the feed's own `published`
# field. Discarded until 2026-09-07, which made publication_date default to
# now() -- the moment of the SCRAPE, not of the story. Everything backfilled
# in one pass therefore landed on a single timestamp, and same-day filtering,
# which is the whole design of the sentiment read, was filtering on when we
# happened to look.
_LAST_PUBLISHED: dict = {}


# TWO SOURCES, BECAUSE THEY DO DIFFERENT JOBS.
#
# Polygon serves PER-TICKER news and cannot serve the macro tape. Measured
# 2026-09-12, both ways:
#
#     ticker=QQQ, 12 days    8 articles, every one an ETF comparison --
#                            "Should Schwab U.S. Large-Cap Growth ETF (SCHG)
#                            Be on Your Investing Radar?"
#     market-wide, 50 rows   0 matched any of the 114 MACRO_TERMS
#
# Which is the same finding symbol_news.py already records: QQQ is not a
# company, a ticker feed returns fund-comparison articles for it, and what
# moves it is rates, yields, oil, jobs and geopolitics. That is what
# MACRO_TERMS was built to match and it needs a general wire to match against.
#
# So: Polygon for the eleven single names, and these two feeds for the macro
# tape alone. No per-symbol RSS, no alias matching against a scrape -- those
# were deleted and stay deleted. This is the narrowest source that closes the
# gap, and without it TRADING_NEWS_DIRECTION is a switch that is on and does
# nothing, which is the failure mode this whole evening was spent removing.
#
# CHECK A FEED'S DATES, NOT ITS STATUS CODE. mw_marketpulse answered 200 for
# months while serving headlines a year old, so _feed_is_stale() below refuses
# a feed whose newest item is older than a day and says so.
MACRO_FEEDS = [
    "https://feeds.content.dowjones.io/public/rss/mw_topstories",
    "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114",
]
MACRO_FEED_ENTRIES = int(os.getenv("TRADING_MACRO_FEED_ENTRIES", "15"))
MACRO_FEED_MAX_AGE_H = int(os.getenv("TRADING_MACRO_FEED_MAX_AGE_H", "48"))
HEADLINE_LOOKBACK_HOURS = int(os.getenv("TRADING_HEADLINE_LOOKBACK_H", "24"))
HEADLINE_LIMIT = int(os.getenv("TRADING_HEADLINE_LIMIT", "120"))


def _tracked_symbols() -> list:
    """Names to pull per-symbol news for. The managed list, not the alias map,
    so a symbol the engine stops trading stops being fetched."""
    raw = os.getenv("TRADING_MANAGE_UNDERLYING", "") or ""
    syms = [s.strip().upper() for s in raw.split(",") if s.strip()]
    return syms[:20]


# ---------------------------------------------------------------------------
# Market sentiment agent
# ---------------------------------------------------------------------------


def macro_headlines() -> list:
    """The macro tape: (title, source, published) from the general wires.

    CALLED BY news_hourly.py ONLY, never from the trading cycle. The cycle
    reads the store; keeping network I/O out of the per-minute path is the
    same constraint that made Polygon hourly, and section 55 records what an
    unguarded call in that path cost: three cycles at the open with seven
    positions live.

    A STALE FEED IS REFUSED AND NAMED. mw_marketpulse answered 200, parsed
    cleanly and served July-2025 headlines for months; nothing logged because
    nothing errored. Freshness is the only check that would have caught it.
    """
    import calendar
    from datetime import datetime as _dt, timezone as _tz

    try:
        import feedparser
    except ImportError:
        logger.warning("feedparser missing — no macro headlines this sweep.")
        return []

    def _published(entry):
        for attr in ("published_parsed", "updated_parsed"):
            t = getattr(entry, attr, None)
            if t:
                try:
                    return _dt.fromtimestamp(calendar.timegm(t), tz=_tz.utc)
                except Exception:
                    pass
        return None

    out = []
    for url in MACRO_FEEDS:
        name = "MARKETWATCH" if "dowjones" in url else "CNBC"
        try:
            feed = feedparser.parse(url)
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s feed failed to parse: %s", name, exc)
            continue
        entries = list(feed.entries[:MACRO_FEED_ENTRIES])
        if not entries:
            logger.warning("%s returned no entries — treating as dead.", name)
            continue
        newest = max((p for p in (_published(e) for e in entries) if p), default=None)
        if newest is not None:
            age_h = (_dt.now(_tz.utc) - newest).total_seconds() / 3600.0
            if age_h > MACRO_FEED_MAX_AGE_H:
                logger.warning(
                    "%s newest item is %.0f hours old — feed is STALE, skipping. "
                    "This is the check mw_marketpulse needed.", name, age_h)
                continue
        for e in entries:
            title = getattr(e, "title", None)
            if title:
                out.append((title, name, _published(e)))
    return out


def _stored_headlines() -> List[str]:
    """Recent headlines READ FROM THE STORE, with no network call.

    scripts/news_hourly.py is the only fetcher: it pulls ticker-tagged
    articles from Polygon once an hour and writes them to
    market_news_vectors. This reads what that job stored.

    IT HAD TO STOP FETCHING, not merely change source. This is called inside
    the per-minute trading cycle, and Polygon's free tier allows five calls a
    minute across twelve tickers -- one cycle would exhaust it. The same
    constraint removes network I/O from the hot path, which section 55 records
    the cost of: three cycles lost at the open with seven positions live.

    WHAT DROPPING RSS BUYS. Articles arrive ticker-tagged, so the alias layer
    and every bug it produced goes with it: ALIASES["SNDK"] that could not see
    a sector story, SECTOR_TERMS that matched 0 of 236 headlines, and
    mw_marketpulse answering 200 for months while serving headlines a year
    old. A dead feed and a quiet news day were indistinguishable here; a
    Polygon 429 is logged by name.
    """
    _LAST_SOURCES.clear()
    _LAST_PUBLISHED.clear()
    out: List[str] = []
    try:
        import psycopg2

        dsn = (os.getenv("DATABASE_URL", "")
               .replace("postgresql+psycopg2://", "postgresql://")
               .replace("postgresql+asyncpg://", "postgresql://"))
        with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT headline_text, source, publication_date "
                "FROM market_news_vectors "
                "WHERE publication_date >= now() - make_interval(hours => %s) "
                "ORDER BY publication_date DESC LIMIT %s",
                (HEADLINE_LOOKBACK_HOURS, HEADLINE_LIMIT),
            )
            for text, source, published in cur.fetchall():
                out.append(text)
                if source:
                    _LAST_SOURCES[text] = source
                if published:
                    _LAST_PUBLISHED[text] = published
    except Exception:
        # Non-fatal, exactly as the scrape was: the cycle continues on the
        # objective macro terms, which is where the measured signal lives.
        logger.warning("Could not read stored headlines — continuing without them.",
                       exc_info=True)
    return out


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
        # GUARDED, like the crude fetch above and every other network call in
        # this agent -- because on 2026-09-03 this one was not.
        #
        #   09:30  ERROR cycle failed
        #   anthropic.OverloadedError: 529 - overloaded_error
        #     nodes.py:1460 market_signals_agent
        #     vector_store.py:82 query_similar_headlines
        #
        # The exception propagated out of the LangGraph node, through
        # run_trading_cycle, and killed three whole cycles at the open: no
        # indicators, no exit checks, no orphan review, on a morning with
        # seven live positions. A HEADLINE LOOKUP TOOK DOWN EXECUTION.
        #
        # This half of the agent is observational -- it feeds a sentiment
        # verdict whose LLM gate is off by default (TRADING_MACRO_LLM_GATE),
        # and the objective terms below decide the macro read regardless.
        # Losing it costs context; losing the cycle costs the stop.
        #
        # Degrades to no headlines rather than no cycle. The verdict then
        # rests on breadth, VIX and yields, which is where the evidence in
        # this file says the signal actually lives.
        # AND IT LATCHES, because degrading gracefully once a minute is still
        # once a minute. Voyage's free tier allows 3 requests a minute; this
        # block makes two per cycle and news_hourly embeds its corpus on top,
        # so the quota is structurally unreachable and every refusal arrived
        # as a full traceback: 71 of them in one session, burying the four
        # lines that actually mattered.
        #
        # The retry could never succeed either. A rate limit that is a
        # PLAN limit does not clear in sixty seconds, so the call was paying
        # latency on every cycle to be refused again.
        #
        # So: one warning, then quiet for EMBED_COOLDOWN_S. The read is
        # observational -- the comment above says the objective terms decide
        # the macro verdict regardless -- so standing it down costs context
        # and nothing else. It re-arms on its own rather than needing a
        # restart, which matters if the quota is ever raised mid-session.
        global _EMBED_DEAD_UNTIL
        if time.time() < _EMBED_DEAD_UNTIL:
            headlines, similar_past_headlines = [], []
        else:
            try:
                headlines = _stored_headlines()
                embeddings = VoyageEmbeddings()
                if headlines:
                    await store_headlines(headlines, embeddings)
                similar_past_headlines = (
                    await query_similar_headlines(headlines, embeddings, top_k=3)
                    if headlines else []
                )
            except Exception as exc:
                _EMBED_DEAD_UNTIL = time.time() + EMBED_COOLDOWN_S
                logger.warning(
                    "Headline read failed (%s) — standing it down for %.0f "
                    "minutes and continuing on the objective macro terms. "
                    "The cycle is NOT abandoned.",
                    type(exc).__name__, EMBED_COOLDOWN_S / 60.0,
                )
                headlines, similar_past_headlines = [], []

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

    # THE MACRO LLM CALL IS GONE (2026-09-13), at the operator's instruction.
    # The prompt below is kept because it documents exactly what the four
    # objective terms are being asked to stand in for, and because a FinBERT
    # read of the macro tape would answer the same question.
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
        # The SECOND unguarded Anthropic call in this agent, and the same
        # failure as the headline lookup above: a 529 here propagated out of
        # the node and killed the cycle on 2026-09-03.
        #
        # Degrades to BAD, not GOOD, and that choice is deliberate. Line ~1600
        # reads `llm_verdict == "GOOD" or not MACRO_LLM_GATE`, so with the gate
        # OFF -- its deployed state -- the value is inert and this costs
        # nothing. With the gate ON, an unknown macro read must not be allowed
        # to PERMIT a bullish entry: refusing on an outage is recoverable,
        # entering on one is not. BAD is therefore safe in both configurations,
        # which is the property worth having in a fallback nobody will
        # re-examine for months.
        #
        # Not cached either: _write_macro_cache would persist a verdict the
        # model never gave and suppress the retry for the cache's whole life.
        # NOT GRADED, AND NOT WRITTEN. The model that produced this verdict
        # was removed; nothing has replaced it yet. The four objective terms
        # below -- breadth level, breadth trend, VIX level, VIX velocity,
        # yield velocity -- carry the macro read on their own, which is the
        # configuration the engine has actually been running since
        # TRADING_MACRO_LLM_GATE was switched off on 2026-08-25.
        #
        # WHY NOT KEEP DEGRADING TO BAD. That fallback was correct while the
        # call existed and could fail transiently: with the gate ON an unknown
        # macro read must not PERMIT a bullish entry. But there is no call to
        # fail now, so "BAD" would no longer mean "the model is down", it
        # would be a permanent fabricated verdict written to
        # trading_macro_verdicts every hour -- and macro_outcome.py measures
        # that table. A dead API key was already doing this: every cycle since
        # the key expired logged "treating as BAD" and recorded it.
        #
        # NOT_GRADED is inert at line ~1600 the same way BAD is while the gate
        # is off. If TRADING_MACRO_LLM_GATE is ever switched back on with no
        # grader wired in, it refuses rather than permits -- the same safe
        # direction, without the fabricated data.
        llm_verdict, llm_confidence = "NOT_GRADED", 0.0
        llm_risk_factor = "no macro grader configured"

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

    # Each term named, so the log records WHICH one refused rather than only
    # that something did.
    #
    # This was inferred by elimination on 2026-08-25 and should not have had
    # to be. Over 08-24 and 08-25, cycles where every objective term passed
    # -- breadth positive, not collapsing, yields quiet, VIX 15.7 against a
    # 22.0 ceiling and -0.76% against a 10% spike limit:
    #
    #     2026-08-24   55 such cycles,  0 GOOD   (100% refused)
    #     2026-08-25   31 such cycles, 11 GOOD   ( 65% refused)
    #
    # By elimination the LLM verdict refused all 55 on a day QQQ rose $6.50
    # off its low. That is a real finding and it took a database query and a
    # process of elimination to reach. One field makes it readable.
    # CRUDE, added 2026-09-06 at the account owner's direction and knowingly
    # against the only measurement of it. sweep.py macro bucketed the engine's
    # own daily P&L by each macro input at the decision, 60 sessions:
    #
    #     crude by 10:15  n=49   down -7.94/day   flat +16.61/day   up -2.15/day
    #     crude by 13:30  n=49   down +3.00/day   flat +26.12/day   up -21.40/day
    #
    # The MIDDLE bucket is best on both rows, which is the signature of noise
    # rather than a directional effect, and ~17 sessions a bucket cannot carry
    # a live gate either way. Rising crude IS the worst bucket by 13:30, which
    # is the reading this gate acts on; it is not the worst by 10:15.
    #
    # Set TRADING_CRUDE_SPIKE_PCT=0 to disable without a deploy. Recorded here
    # so that when this is re-examined the evidence it was added against is
    # beside it, not in a commit message nobody will find.
    crude_spiking = (
        oil is not None and CRUDE_SPIKE_PCT > 0 and oil.change_pct >= CRUDE_SPIKE_PCT
    )
    if crude_spiking:
        logger.warning(
            "Crude spiking: %.2f, %+.2f%% from today's open — forcing risk-off.",
            oil.level, oil.change_pct,
        )

    gates = {
        "breadth": breadth_is_bullish,
        "vix_level": vix.level < VIX_LEVEL_MAX,
        "vix_spike": vix.change_pct < VIX_SPIKE_PCT,
        "yields": not yields_spiking,
        "crude": not crude_spiking,
        "llm": llm_verdict == "GOOD" or not MACRO_LLM_GATE,
    }
    failed = [name for name, ok in gates.items() if not ok]
    sentiment = "GOOD" if not failed else "BAD"
    if failed:
        logger.info("Macro BAD — refused by: %s", ", ".join(failed))

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
        # Which AND-term refused, comma-joined, empty when GOOD. The whole
        # point is that "BAD" on its own is unactionable: breadth turning
        # negative and a headline model disliking the tape are different
        # facts with different fixes.
        "macro_block_reason": ",".join(failed),
        "macro_halt": halt,
        # The breadth that just gated this decision, as numbers rather than as
        # a sentence. Both gates below read these, and neither could be
        # re-examined afterwards while the values were discarded.
        "breadth_addq": float(breadth.addq),
        "breadth_advancers": int(breadth.advancers),
        "breadth_decliners": int(breadth.decliners),
        "breadth_net_ratio": round(breadth_trend.net_ratio, 4),
        "breadth_drawdown": round(breadth_trend.drawdown_from_recent_peak, 4),
        "breadth_collapsing": bool(breadth_is_collapsing),
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


def _qqq_news_verdict() -> "str | None":
    """Today's QQQ macro news verdict from news_verdicts (cached a minute)."""
    import time as _t
    now = _t.time()
    if _news_cache.get("at", 0) > now - 60:
        return _news_cache.get("v")
    v = None
    try:
        import psycopg2
        from .symbol_news import _dsn
        with psycopg2.connect(_dsn()) as conn, conn.cursor() as cur:
            cur.execute("SELECT verdict FROM news_verdicts WHERE symbol='QQQ' "
                        "AND trading_day=%s", (datetime.now(NY).date(),))
            row = cur.fetchone()
            v = (row[0] or "").upper() if row else None
    except Exception:
        logger.warning("QQQ news verdict unreadable -- the macro gate ignores news.", exc_info=True)
    _news_cache.update(at=now, v=v)
    return v


def macro_refusal(bullish: bool, state: dict) -> "str | None":
    """Why the macro gate refuses this touch, or None (section 269).

    Calls: 10Y not up > MACRO_YIELD_BPS, crude not up > MACRO_OIL_PCT, news not
    bearish. Puts: the mirror. A missing reading does not block."""
    if not BAND_TOUCH_MACRO_GATE:
        return None
    bps, oil = state.get("tnx_change_bps"), state.get("oil_change_pct")
    news = _qqq_news_verdict()
    y, o = BAND_TOUCH_MACRO_YIELD_BPS, BAND_TOUCH_MACRO_OIL_PCT
    if bullish:
        if bps is not None and float(bps) > y:
            return f"macro gate: 10Y up {float(bps):+.1f}bp (> {y:g}bp) -- no call."
        if oil is not None and float(oil) > o:
            return f"macro gate: crude up {float(oil):+.2f}% (> {o:g}%) -- no call."
        if news in _NEWS_BEARISH:
            return f"macro gate: QQQ macro news {news} -- no call."
    else:
        if bps is not None and float(bps) < -y:
            return f"macro gate: 10Y down {float(bps):+.1f}bp (< -{y:g}bp) -- no put."
        if oil is not None and float(oil) < -o:
            return f"macro gate: crude down {float(oil):+.2f}% (< -{o:g}%) -- no put."
        if news in _NEWS_BULLISH:
            return f"macro gate: QQQ macro news {news} -- no put."
    return None


def macd_refusal(bullish: bool, band: "dict | None") -> "str | None":
    """Why the MACD check refuses this touch, or None (section 269)."""
    if not BAND_TOUCH_MACD_CHECK or not band or band.get("macd_hist") is None:
        return None
    h, p = band["macd_hist"], band["macd_hist_prev"]
    if bullish and not h > p:
        return f"MACD check: 1-min histogram {h:+.3f} not rising (was {p:+.3f}) -- no call yet."
    if not bullish and not h < p:
        return f"MACD check: 1-min histogram {h:+.3f} not falling (was {p:+.3f}) -- no put yet."
    return None


def trend_refusal(bullish: bool, band: "dict | None") -> "str | None":
    """Why the trend check refuses this touch, or None (section 268)."""
    if not BAND_TOUCH_TREND_CHECK or not band or band.get("slope") is None:
        return None
    sl = band["slope"]
    if not bullish and sl > BAND_TOUCH_TREND_MIN:
        return (f"trend check: the 20-SMA rose ${sl:.2f} in {BAND_TOUCH_TREND_BARS} min "
                f"(> ${BAND_TOUCH_TREND_MIN:.2f}) -- no put against a rising market.")
    if bullish and sl < -BAND_TOUCH_TREND_MIN:
        return (f"trend check: the 20-SMA fell ${-sl:.2f} in {BAND_TOUCH_TREND_BARS} min "
                f"(> ${BAND_TOUCH_TREND_MIN:.2f}) -- no call against a falling market.")
    return None


def _touch_target_hit(position, band: "dict | None") -> bool:
    """QQQ back at the 1-minute 20-SMA, on the side the trade was opened for."""
    if not band:
        return False
    if position.strategy == BULL_CALL_SPREAD:
        return band["spot"] >= band["mid"]
    if position.strategy == BEAR_PUT_SPREAD:
        return band["spot"] <= band["mid"]
    return False


def execution_risk_agent(state: TradingState, broker: MockBrokerClient = None) -> dict:
    if os.path.exists(KILL_SWITCH_PATH):
        logger.warning("KILL_SWITCH.txt present — halting all algorithmic execution.")
        return {"execution_status": "HALTED", "buy_more_count": state.get("buy_more_count", 0)}

    broker = broker or default_mock_broker()
    halt = bool(state.get("macro_halt"))
    position = broker.get_open_position()
    force_close = is_past_force_close()
    in_warmup = _is_within_opening_warmup()

    action = "HOLD"
    exit_reason = ""
    playbook = ""

    # ---- Managing the open position -------------------------------------
    # Exit order (section 261): force close -> engine take-profit -> QQQ back
    # at the 1-minute 20-SMA -> profit lock (section 271) -> stop with
    # confirmation -> hold. A row left by a retired window still gets the stop
    # and the force close.
    if position is not None:
        return_pct = position.return_pct
        _tp, stop_pct, _ro = thresholds_for(
            position.playbook, (TAKE_PROFIT_PCT, STOP_LOSS_PCT, STOP_LOSS_PCT))
        # Ticked every cycle, before the ladder, so recoveries reach the clock.
        stop_ok = _stop_confirmed(position, return_pct, stop_pct)
        touch_pos = (getattr(position, "playbook", "") or "").startswith("BAND_TOUCH")
        band = _band_touch() if (touch_pos and not force_close) else None
        engine_tp = PB.ENGINE_TAKE_PROFIT_PCT
        lock_floor = XR.opt_float("TRADING_ENGINE_PROFIT_LOCK_PCT")
        lock_arm = XR.opt_float("TRADING_ENGINE_PROFIT_LOCK_ARM_PCT")
        if lock_arm is None:
            lock_arm = engine_tp
        peak = max(position.peak_return_pct or 0.0, return_pct)

        if force_close:
            broker.sell_all(position.underlying)
            action, exit_reason = "SELL_ALL", "FORCE_CLOSE"
        elif (engine_tp is not None and not is_credit(position.strategy)
              and return_pct >= engine_tp):
            logger.info("Engine take-profit: %s at %+.1f%% reached the %+.0f%% setting — booking.",
                        position.strategy, return_pct, engine_tp)
            broker.sell_all(position.underlying)
            action, exit_reason = "SELL_ALL", "TAKE_PROFIT"
        elif (touch_pos and _touch_target_hit(position, band)
              and return_pct >= BAND_TOUCH_MIN_PROFIT_PCT):
            logger.info("Band touch: QQQ %.2f is back at the 20-SMA %.2f — booking %s at %+.1f%%.",
                        band["spot"], band["mid"], position.strategy, return_pct)
            broker.sell_all(position.underlying)
            action, exit_reason = "SELL_ALL", "TAKE_PROFIT"
        elif (not is_credit(position.strategy)
              and XR.profit_lock(peak, return_pct, lock_arm, lock_floor)):
            logger.info("Engine profit lock: %s peaked %+.1f%% (lock arms at %+.0f%%) and is back "
                        "to %+.1f%% (lock %+.0f%%) — booking what is left.",
                        position.strategy, peak, lock_arm, return_pct, lock_floor)
            broker.sell_all(position.underlying)
            action, exit_reason = "SELL_ALL", "PROFIT_LOCK"
        elif return_pct <= stop_pct and stop_ok:
            broker.sell_all(position.underlying)
            action, exit_reason = "SELL_ALL", "STOP_LOSS"
        else:
            logger.info("Holding %s at %+.1f%% (stop %+.0f%%%s)%s.",
                        position.strategy, return_pct, stop_pct,
                        f", take-profit {engine_tp:+.0f}%" if engine_tp is not None else "",
                        f", QQQ {band['spot']:.2f} vs 20-SMA {band['mid']:.2f}" if band else "")

    # ---- Entry: the 1-minute band touch ----------------------------------
    # Checked after management, so a cycle that books the target can re-enter
    # on the next touch without sitting out a minute. Never straight after a
    # stop: the loss cooldown decides that.
    may_reenter = position is None or exit_reason in ("TAKE_PROFIT", "PROFIT_LOCK")
    entry_window = window_for()
    if (broker.get_open_position() is None and may_reenter and not in_warmup
            and entry_window is not None):
        tier, bullish = None, False
        tb = _band_touch()
        if tb and tb["touch"] and not halt:
            _bull = tb["touch"] == "LOWER"
            if win_pause_active() or entry_cap_reached():
                pass
            elif blocked_direction() == ("bullish" if _bull else "bearish"):
                pass
            elif macro_refusal(_bull, state):
                logger.info("Band touch: %s", macro_refusal(_bull, state))
            elif trend_refusal(_bull, tb):
                logger.info("Band touch: %s", trend_refusal(_bull, tb))
            elif macd_refusal(_bull, tb):
                logger.info("Band touch: %s", macd_refusal(_bull, tb))
            else:
                tier, bullish = "TOUCH", _bull
        if tb:
            if tier:
                verdict = f"{tb['touch']} touch -> {'call' if bullish else 'put'} spread"
            elif tb["touch"]:
                verdict = f"{tb['touch']} touch refused" + (" (macro halt)" if halt else "")
            else:
                verdict = "inside the band"
            logger.info("Band touch read: QQQ %.2f, 1-min band %.2f / %.2f / %.2f — %s.",
                        tb["spot"], tb["lower"], tb["mid"], tb["upper"], verdict)

        if tier is not None:
            window = window_for_direction(bullish)
            eq = current_equity(POSITION_BUDGET)
            # THE QQQ 0DTE BUCKET SWITCH (section 227). Off means no NEW engine
            # entries; an open position is still managed. Read per cycle.
            bucket_on = os.getenv("TRADING_BUCKET_QQQ_0DTE", "false").lower() == "true"
            streak = 0
            if not bucket_on:
                window = None
                action = "BUCKET_OFF"
                logger.info("QQQ 0DTE bucket is OFF (TRADING_BUCKET_QQQ_0DTE) — no new entry.")
            elif eq.halted:
                window = None
                action = "HALTED_DAILY_LOSS"
            else:
                streak = consecutive_losses_today()
                if streak >= MAX_CONSECUTIVE_LOSSES:
                    logger.warning(
                        "%d consecutive losing trades today — standing down for the session.", streak,
                    )
                    window = None
                    action = "HALTED_LOSS_STREAK"

            if window is not None:
                spot = float(tb["spot"])
                atm_strike = round_to_strike(spot)
                try:
                    chain = fetch_option_chain()
                except Exception:
                    logger.exception("Chain fetch failed — pricing from the model.")
                    chain = {}
                strategy = BULL_CALL_SPREAD if bullish else BEAR_PUT_SPREAD
                long_strike, short_strike = strikes_for(window, atm_strike, bullish)
                # Buying a vertical fills at its natural ask; the model is the
                # fallback when the chain cannot price it.
                model_mid = estimate_spread_value(strategy, long_strike, short_strike, spot)
                net_debit = fill_price(model_mid, "buy")
                market = chain_vertical(chain, option_type_for(strategy),
                                        long_strike, short_strike) if chain else None
                if market is not None and market["ask"] > 0:
                    net_debit = market["ask"]
                quantity = broker.estimate_spread_quantity(eq.equity * ENTRY_FRACTION, net_debit)

                # Size to what the broker will actually be given once orders are
                # live, so the position, its P&L and the daily cap describe
                # what was sent.
                if tradier_orders.LIVE_ORDERS and quantity > tradier_orders.MAX_CONTRACTS:
                    logger.info("Sizing %d contracts down to the %d live-order cap.",
                                quantity, tradier_orders.MAX_CONTRACTS)
                    quantity = tradier_orders.MAX_CONTRACTS

                # Half size after a loss.
                if streak > 0 and quantity > 1:
                    logger.info("Re-entry after %d loss(es) today — halving size from %d to %d contracts.",
                                streak, quantity, quantity // 2)
                    quantity = quantity // 2

                # Risk allocation: how much of the day's loss budget this trade
                # may consume at its stop.
                risk_share = REENTRY_RISK_SHARE if streak > 0 else DEFAULT_RISK_SHARE
                stop_pct_for_entry = thresholds_for(
                    window.name, (TAKE_PROFIT_PCT, STOP_LOSS_PCT, STOP_LOSS_PCT))[1]
                risk_per_contract = abs(stop_pct_for_entry) / 100.0 * net_debit * 100
                risk_budget = risk_share * eq.daily_loss_limit
                if risk_per_contract > 0:
                    max_by_risk = int(risk_budget // risk_per_contract)
                    if max_by_risk < quantity:
                        logger.info(
                            "Risk allocation: %.0f%% of the $%.0f daily budget ($%.0f); one contract "
                            "stops at $%.0f — sizing %d contracts, not %d.",
                            risk_share * 100, eq.daily_loss_limit, risk_budget,
                            risk_per_contract, max_by_risk, quantity)
                        quantity = max_by_risk

                # The tail cap: the premium paid is what a debit spread loses
                # when the stop does not get a chance to work.
                structural_per_contract = net_debit * 100
                if quantity > 0 and structural_per_contract > 0:
                    max_by_tail = int((MAX_POSITION_RISK_PCT * eq.equity) // structural_per_contract)
                    if max_by_tail < quantity:
                        logger.info(
                            "Tail cap: %d contracts would put $%.0f at risk, above %.0f%% of $%.0f "
                            "equity — sizing %d.", quantity, quantity * structural_per_contract,
                            MAX_POSITION_RISK_PCT * 100, eq.equity, max_by_tail)
                        quantity = max_by_tail

                # SECTION 249: AT LEAST ONE CONTRACT, when the budget covers it.
                if (quantity <= 0 and MIN_ONE_CONTRACT and structural_per_contract > 0
                        and structural_per_contract <= POSITION_BUDGET):
                    logger.info(
                        "Sizing: the fractional caps round to 0, but one contract ($%.0f at "
                        "risk) fits the $%.0f budget — sizing 1 (TRADING_MIN_ONE_CONTRACT).",
                        structural_per_contract, POSITION_BUDGET)
                    quantity = 1
                # SIZE AGAINST THE MONEY IN THE ACCOUNT (section 239).
                quantity = cap_to_buying_power(quantity, structural_per_contract)
                if quantity <= 0:
                    logger.info(
                        "Sizing: %s sized to 0 contracts at $%.2f — entry %.0f%% of $%.0f equity; "
                        "risk $%.2f of the $%.2f daily limit vs $%.0f at the stop; tail cap $%.0f "
                        "vs $%.0f per contract. No entry.%s",
                        window.name, net_debit, ENTRY_FRACTION * 100, eq.equity, risk_budget,
                        eq.daily_loss_limit, risk_per_contract, MAX_POSITION_RISK_PCT * eq.equity,
                        structural_per_contract,
                        "" if MIN_ONE_CONTRACT else
                        " (TRADING_MIN_ONE_CONTRACT would size 1 if one fits the budget.)")

                if quantity > 0:
                    try:
                        log_price_divergence(option_type_for(strategy), long_strike, short_strike,
                                             model_mid, f"entry {window.name}")
                    except Exception:
                        logger.exception("Chain divergence log failed — entering anyway.")
                    playbook = f"{window.name}:{tier}"
                    tp_note = (f", take-profit {PB.ENGINE_TAKE_PROFIT_PCT:+.0f}%"
                               if PB.ENGINE_TAKE_PROFIT_PCT is not None else "")
                    logger.info(
                        "Entering %s via %s: %d contracts, long %.1f / short %.1f at $%.2f "
                        "(equity $%.2f, exit at the 20-SMA, stop %+.0f%%%s)",
                        "BULL" if bullish else "BEAR", playbook, quantity, long_strike,
                        short_strike, net_debit, eq.equity, stop_pct_for_entry, tp_note)
                    if bullish:
                        broker.place_bull_call_spread("QQQ", quantity, long_strike, short_strike,
                                                      net_debit, playbook)
                        action = "BUY_CALL_SPREAD"
                    else:
                        broker.place_bear_put_spread("QQQ", quantity, long_strike, short_strike,
                                                     net_debit, playbook)
                        action = "BUY_PUT_SPREAD"

    return {
        "execution_status": action,
        "exit_reason": exit_reason,
        "playbook": playbook,
        "buy_more_count": state.get("buy_more_count", 0),
    }
