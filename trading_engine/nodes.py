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

from .broker import MockBrokerClient, default_mock_broker
from .data_feed import fetch_market_breadth, fetch_qqq_bars, fetch_vix
from .state import TradingState

logger = logging.getLogger(__name__)

KILL_SWITCH_PATH = "KILL_SWITCH.txt"

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

    is_divergent = breadth.addq > 400 and breadth.tickq < -800
    if is_divergent:
        logger.warning("🚨 MARKET TRAP DETECTED — $ADDQ=%.1f $TICKQ=%.1f", breadth.addq, breadth.tickq)

    llm = ChatAnthropic(model="claude-opus-5", max_tokens=1024).with_structured_output(MarketSentimentOutput)
    prompt = (
        "You are a macro risk classifier for a same-day QQQ options trading system. "
        "Classify today's market risk as GOOD (safe to hold/enter a bullish position) or BAD (risk-off).\n\n"
        f"Nasdaq Advance-Decline Difference ($ADDQ): {breadth.addq}\n"
        f"Nasdaq Net Tick Index ($TICKQ): {breadth.tickq}\n"
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
        not is_divergent
        and breadth.addq > 300
        and breadth.tickq > -200
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
        return_pct = ((position.current_market_premium - position.entry_cost_basis) / position.entry_cost_basis) * 100

        # Rule A: Take Profit
        if return_pct >= 10.0:
            action = "SELL_ALL"
        # Rule B / C: Stop Loss vs. Buy More
        elif return_pct <= -10.0:
            if (
                not past_cutoff
                and sentiment == "GOOD"
                and count < 3
                and available_cash >= (position.current_market_premium * 100)
            ):
                action = "BUY_MORE"
            else:
                # Past the 2PM EST cutoff, or sentiment/cash/count don't clear the bar — fall back to stop loss.
                action = "SELL_ALL"
    else:
        if macd == "BULLISH" and sma == "ABOVE_SMA" and bb == "LOWER_BAND" and sentiment == "GOOD":
            action = "BUY_CALL"

    return {
        "execution_status": action,
        "buy_more_count": count + 1 if action == "BUY_MORE" else count,
    }
