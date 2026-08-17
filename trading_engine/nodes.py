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

from .broker import ITM_OFFSET, MockBrokerClient, default_mock_broker, round_to_strike
from .data_feed import fetch_market_breadth, fetch_qqq_bars, fetch_vix
from .state import TradingState

logger = logging.getLogger(__name__)

KILL_SWITCH_PATH = "KILL_SWITCH.txt"

# Debit-spread strategy config — see execution_risk_agent below.
POSITION_BUDGET = float(os.getenv("TRADING_POSITION_BUDGET", "1000"))
TAKE_PROFIT_PCT = float(os.getenv("TRADING_TAKE_PROFIT_PCT", "20.0"))
STOP_LOSS_PCT = float(os.getenv("TRADING_STOP_LOSS_PCT", "-10.0"))  # unchanged from the original spec — only take-profit moved to 20%

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
    """Closing price vs. the 20- and 50-period simple moving averages
    (computed over the fetched intraday bar series)."""
    bars = fetch_qqq_bars()
    close = bars["Close"]

    sma20 = close.rolling(window=20).mean().iloc[-1]
    sma50 = close.rolling(window=50).mean().iloc[-1] if len(close) >= 50 else sma20
    last_close = close.iloc[-1]

    trend = "ABOVE_SMA" if (last_close > sma20 and last_close > sma50) else "BELOW_SMA"
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
    # $ADDQ is self-computed from NASDAQ_BREADTH_BASKET (see data_feed.py) —
    # scaled to that basket's size, not the original spec's full-market
    # threshold, since this is a smaller representative sample.
    advance_ratio_threshold = float(os.getenv("BREADTH_ADVANCE_RATIO_THRESHOLD", "0.15"))
    breadth_is_bullish = breadth.addq > (breadth.basket_size * advance_ratio_threshold)

    llm = ChatAnthropic(model="claude-opus-5", max_tokens=1024).with_structured_output(MarketSentimentOutput)
    prompt = (
        "You are a macro risk classifier for a same-day QQQ options trading system. "
        "Classify today's market risk as GOOD (safe to hold/enter a bullish position) or BAD (risk-off).\n\n"
        f"Nasdaq breadth (self-computed from a {breadth.basket_size}-stock basket): "
        f"{breadth.advancers} advancing, {breadth.decliners} declining, {breadth.unchanged} unchanged "
        f"(net {breadth.addq:+.0f})\n"
        f"CBOE Volatility Index (VIX): {vix}\n\n"
        f"Today's headlines:\n" + "\n".join(f"- {h}" for h in headlines[:20]) + "\n\n"
        + (
            "Similar historical headlines and what followed (for context):\n"
            + "\n".join(f"- {h}" for h in similar_past_headlines)
            if similar_past_headlines else ""
        )
    )
    llm_result: MarketSentimentOutput = await llm.ainvoke(prompt)

    sentiment = "GOOD" if (
        breadth_is_bullish
        and vix < 22.0
        and llm_result.verdict == "GOOD"
    ) else "BAD"

    return {"market_sentiment": sentiment}


# ---------------------------------------------------------------------------
# Execution risk agent (deterministic rule engine)
# ---------------------------------------------------------------------------


def _is_past_cutoff(cutoff_hour: int = 14) -> bool:
    now_est = datetime.now(ZoneInfo("America/New_York"))
    return now_est.hour >= cutoff_hour


def execution_risk_agent(state: TradingState, broker: MockBrokerClient = None) -> dict:
    if os.path.exists(KILL_SWITCH_PATH):
        logger.warning("KILL_SWITCH.txt present — halting all algorithmic execution.")
        return {"execution_status": "HALTED", "buy_more_count": state.get("buy_more_count", 0)}

    broker = broker or default_mock_broker()
    sentiment = state.get("market_sentiment")
    macd = state.get("macd_signal")
    sma = state.get("sma_trend")
    bb = state.get("bollinger_zone")
    count = state.get("buy_more_count", 0)

    position = broker.get_open_position()
    available_cash = broker.get_available_cash()
    past_cutoff = _is_past_cutoff()

    action = "HOLD"

    if position is not None:
        return_pct = position.return_pct

        # Rule A: Take Profit (+20%)
        if return_pct >= TAKE_PROFIT_PCT:
            broker.sell_all(position.underlying)
            action = "SELL_ALL"
        # Rule B / C: Stop Loss (-10%, unchanged) vs. Buy More
        elif return_pct <= STOP_LOSS_PCT:
            if (
                not past_cutoff
                and sentiment == "GOOD"
                and count < 3
                and available_cash >= (position.current_net_value * 100)
            ):
                broker.place_buy_more(position.underlying, position.quantity)
                action = "BUY_MORE"
            else:
                # Past the 2PM EST cutoff, or sentiment/cash/count don't clear the bar — fall back to stop loss.
                broker.sell_all(position.underlying)
                action = "SELL_ALL"
    else:
        # Bullish: bull call debit spread (long ITM call, short ATM call).
        # Bearish: bear put debit spread (long ITM put, short ATM put) — mirrored
        # trigger, same market_sentiment=GOOD gate (a calm macro environment is
        # required to open a new position either direction).
        bullish = macd == "BULLISH" and sma == "ABOVE_SMA" and bb == "LOWER_BAND" and sentiment == "GOOD"
        bearish = macd == "BEARISH" and sma == "BELOW_SMA" and bb == "UPPER_BAND" and sentiment == "GOOD"

        if bullish or bearish:
            spot = float(fetch_qqq_bars()["Close"].iloc[-1])
            atm_strike = round_to_strike(spot)
            quantity = broker.estimate_spread_quantity(POSITION_BUDGET)

            if quantity > 0:
                if bullish:
                    long_strike, short_strike = atm_strike - ITM_OFFSET, atm_strike
                    broker.place_bull_call_spread("QQQ", quantity, long_strike, short_strike)
                    action = "BUY_CALL_SPREAD"
                else:
                    long_strike, short_strike = atm_strike + ITM_OFFSET, atm_strike
                    broker.place_bear_put_spread("QQQ", quantity, long_strike, short_strike)
                    action = "BUY_PUT_SPREAD"

    return {
        "execution_status": action,
        "buy_more_count": count + 1 if action == "BUY_MORE" else count,
    }
