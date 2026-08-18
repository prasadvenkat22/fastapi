"""The five trading graph agents.

macd_agent / sma_agent / bollinger_agent compute technical indicators from the
yfinance QQQ bar series. market_signals_agent combines Tradier breadth data,
VIX, scraped headlines, and a Claude structured-output sentiment call.
execution_risk_agent is the deterministic rule engine that turns all of the
above into a final trading decision.
"""

import logging
import os
from datetime import datetime
from typing import List
from zoneinfo import ZoneInfo

import feedparser
from langchain_anthropic import ChatAnthropic

from GENAI.vector_stores import VoyageEmbeddings
from schemas_pgrs.trading_schema import MarketSentimentOutput

from .breadth_history import RECENT_WINDOW_MINUTES, record_and_summarize
from .playbook import strikes_for, window_for
from .broker import (
    BEAR_PUT_SPREAD,
    BULL_CALL_SPREAD,
    ITM_OFFSET,
    MockBrokerClient,
    default_mock_broker,
    estimate_spread_value,
    round_to_strike,
)
from .data_feed import fetch_market_breadth, fetch_qqq_bars, fetch_vix
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
ENTRY_FRACTION = float(os.getenv("TRADING_ENTRY_FRACTION", "0.4"))

# Cap on scale-ins per position, unchanged from the original hardcoded 3.
MAX_SCALE_INS = int(os.getenv("TRADING_MAX_SCALE_INS", "3"))

# Opening warmup. Entries wait this many minutes after the bell so the
# opening auction's whipsaws don't get read as a trend; position management
# is unaffected and runs from the first cycle.
MARKET_OPEN_HOUR, MARKET_OPEN_MINUTE = 9, 30
WARMUP_MINUTES = int(os.getenv("TRADING_WARMUP_MINUTES", "15"))
# A debit spread cannot be worth more than its width, so the most a position
# can ever gain is (width - entry debit) / entry debit — and the entry debit
# rises through the day as time value drains, which lowers that ceiling as
# the session runs on. Measured on a $3-wide spread: ~62% at the open, ~49%
# by 13:00, ~42% by the 14:00 cutoff. 30% stays reachable at any entry time;
# 50% is structurally impossible after midday, and a target that can't be hit
# isn't a target — the position just rides to the force-close instead.
TAKE_PROFIT_PCT = float(os.getenv("TRADING_TAKE_PROFIT_PCT", "30.0"))
STOP_LOSS_PCT = float(os.getenv("TRADING_STOP_LOSS_PCT", "-10.0"))  # unchanged from the original spec — only take-profit moved to 20%

# Tightened stop that applies only while macro is risk-off (market_sentiment
# == 'BAD'). Riding a losing 0DTE spread all the way to the full -10% stop
# into a deteriorating tape gives up twice the capital for a position whose
# thesis has already broken; cutting at -5% and re-entering later if
# conditions improve is the cheaper path — entries are re-evaluated every
# scheduler cycle anyway, so nothing is permanently forfeited by leaving.
RISK_OFF_STOP_LOSS_PCT = float(os.getenv("TRADING_RISK_OFF_STOP_LOSS_PCT", "-5.0"))

# Macro risk-off thresholds. VIX is judged on both level and session move:
# a spike of this magnitude is treated as risk-off even from a low base.
VIX_LEVEL_MAX = float(os.getenv("TRADING_VIX_LEVEL_MAX", "22.0"))
VIX_SPIKE_PCT = float(os.getenv("TRADING_VIX_SPIKE_PCT", "10.0"))

# Breadth is judged on level and trend alike. Expressed as a drop in net
# breadth ratio (advancers-minus-decliners over basket size) from its peak
# over the recent window: 0.40 is roughly a fifth of the basket flipping
# from advancing to declining inside half an hour — participation draining
# out of a tape that still prints positive.
BREADTH_COLLAPSE_RATIO = float(os.getenv("TRADING_BREADTH_COLLAPSE_RATIO", "0.40"))

# Standard Wilder's RSI(14) thresholds.
RSI_OVERBOUGHT = float(os.getenv("TRADING_RSI_OVERBOUGHT", "70.0"))
RSI_OVERSOLD = float(os.getenv("TRADING_RSI_OVERSOLD", "30.0"))

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

    return {"macd_signal": signal}


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
    return {"sma_trend": trend}


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

    return {"bollinger_zone": zone}


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

    return {"rsi_zone": zone}


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

    llm = ChatAnthropic(model="claude-opus-5", max_tokens=1024).with_structured_output(MarketSentimentOutput)
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
    llm_result: MarketSentimentOutput = await llm.ainvoke(prompt)

    # VIX is gated on level *and* velocity — a sharp intraday spike is
    # risk-off even when the absolute level is still under the ceiling,
    # which is exactly the mid-session regime change a level-only check
    # sleeps through.
    sentiment = "GOOD" if (
        breadth_is_bullish
        and vix.level < VIX_LEVEL_MAX
        and vix.change_pct < VIX_SPIKE_PCT
        and llm_result.verdict == "GOOD"
    ) else "BAD"

    return {"market_sentiment": sentiment}


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


def is_past_force_close(hour: int = 15, minute: int = 45) -> bool:
    """Hard close-out cutoff, independent of P&L — QQQ options expire at
    today's close, so any open spread must be flattened before then rather
    than allowed to ride into expiration (assignment/pin risk on the short
    leg, and an OTM long leg simply expires worthless)."""
    now_est = datetime.now(ZoneInfo("America/New_York"))
    return (now_est.hour, now_est.minute) >= (hour, minute)


def execution_risk_agent(state: TradingState, broker: MockBrokerClient = None) -> dict:
    if os.path.exists(KILL_SWITCH_PATH):
        logger.warning("KILL_SWITCH.txt present — halting all algorithmic execution.")
        return {"execution_status": "HALTED", "buy_more_count": state.get("buy_more_count", 0)}

    broker = broker or default_mock_broker()
    sentiment = state.get("market_sentiment")
    macd = state.get("macd_signal")
    sma = state.get("sma_trend")
    bb = state.get("bollinger_zone")
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

        # Rule Z: same-day expiration hard close — overrides P&L entirely.
        if force_close:
            broker.sell_all(position.underlying)
            action, exit_reason = "SELL_ALL", "FORCE_CLOSE"
        # Rule A: Take Profit (+20%)
        elif return_pct >= TAKE_PROFIT_PCT:
            broker.sell_all(position.underlying)
            action, exit_reason = "SELL_ALL", "TAKE_PROFIT"
        # Rule B / C: Stop Loss (-10%, unchanged) vs. Buy More
        elif return_pct <= STOP_LOSS_PCT:
            # place_buy_more adds `position.quantity` more contracts — it
            # doubles the position — so the affordability check has to price
            # that whole lot. Checking a single contract's cost (as this once
            # did) authorised roughly a 5x larger purchase than it verified,
            # and repeated doubling would have compounded the gap: 5 -> 10 ->
            # 20 -> 40 contracts, each step approved by a one-contract test.
            scale_in_cost = position.current_net_value * 100 * position.quantity
            if (
                not past_cutoff
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
        # already losing. Cut at -5% rather than waiting for the full -10%
        # stop: the setup that justified the entry no longer holds, and a
        # re-entry is available on any later cycle if conditions recover.
        #
        # Deliberately evaluated *after* the -10% stop above so a position
        # that already breached the full stop is still recorded as STOP_LOSS
        # — this rule only owns the band between the two thresholds, which
        # keeps the close reasons feeding the setup vector store honest.
        elif sentiment == "BAD" and return_pct <= RISK_OFF_STOP_LOSS_PCT:
            logger.warning(
                "Risk-off exit: macro sentiment BAD with position at %.2f%% — closing early "
                "rather than riding to the %.1f%% stop.", return_pct, STOP_LOSS_PCT,
            )
            broker.sell_all(position.underlying)
            action, exit_reason = "SELL_ALL", "RISK_OFF"
    elif not past_cutoff and not in_warmup:
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
        bullish_fade = (
            macd == "BULLISH" and sma == "ABOVE_SMA" and bb == "LOWER_BAND"
            and rsi == "OVERSOLD" and sentiment == "GOOD"
        )
        bullish_continuation = (
            macd == "BULLISH" and sma == "ABOVE_SMA" and bb == "UPPER_BAND"
            and rsi == "OVERBOUGHT" and sentiment == "GOOD"
        )
        bullish = bullish_fade or bullish_continuation
        bearish_fade = (
            macd == "BEARISH" and sma == "BELOW_SMA" and bb == "UPPER_BAND"
            and rsi == "OVERBOUGHT" and sentiment == "GOOD"
        )
        bearish_continuation = (
            macd == "BEARISH" and sma == "BELOW_SMA" and bb == "LOWER_BAND"
            and rsi == "OVERSOLD" and sentiment == "GOOD"
        )
        bearish = bearish_fade or bearish_continuation

        if bullish or bearish:
            spot = float(fetch_qqq_bars()["Close"].iloc[-1])
            atm_strike = round_to_strike(spot)

            # Strike placement comes from the time-of-day window, so the same
            # signal produces a leveraged ATM structure during the morning
            # momentum leg and a positive-theta ITM one through the midday
            # lull. window is None outside every window — that is a no-entry
            # period, including any gap left by retiring a strategy.
            window = window_for()
            if window is not None:
                strategy = BULL_CALL_SPREAD if bullish else BEAR_PUT_SPREAD
                long_strike, short_strike = strikes_for(window, atm_strike, bullish)

                # Price the entry with the same model that reprices it next
                # cycle. Sizing uses that price too, or the position costs
                # something other than the budget it was sized against.
                net_debit = estimate_spread_value(strategy, long_strike, short_strike, spot)
                quantity = broker.estimate_spread_quantity(POSITION_BUDGET * ENTRY_FRACTION, net_debit)

                if quantity > 0:
                    playbook = window.name
                    logger.info(
                        "Entering %s via %s: %s %d contracts, long %.1f / short %.1f at $%.2f",
                        "BULL" if bullish else "BEAR", window.name, window.placement,
                        quantity, long_strike, short_strike, net_debit,
                    )
                    if bullish:
                        broker.place_bull_call_spread("QQQ", quantity, long_strike, short_strike, net_debit)
                        action = "BUY_CALL_SPREAD"
                    else:
                        broker.place_bear_put_spread("QQQ", quantity, long_strike, short_strike, net_debit)
                        action = "BUY_PUT_SPREAD"

    return {
        "execution_status": action,
        "exit_reason": exit_reason,
        "playbook": playbook,
        "buy_more_count": count + 1 if action == "BUY_MORE" else count,
    }
