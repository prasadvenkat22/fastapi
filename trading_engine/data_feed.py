"""Live market data for the trading graph.

Price/VIX bars come from yfinance (free, no key required). Market breadth is
self-computed: confirmed live, against both Tradier sandbox and production
with a real account, that Tradier's symbol catalog does not carry $ADDQ or
$TICKQ at all (explicit "unmatched_symbols" response, and a lookup search for
both tickers under several naming variants returned nothing) — so rather than
depend on a vendor that doesn't have this data, we compute our own advance/
decline breadth from a basket of Nasdaq-100 constituents via the same Tradier
quotes endpoint (which does handle ordinary equity symbols fine).

There is no self-computed equivalent for $TICKQ (the real Net Tick Index
needs tick-by-tick trade classification data no snapshot-quote API provides)
— it's intentionally dropped rather than approximated poorly.
"""

import os
from dataclasses import dataclass
from datetime import time
from zoneinfo import ZoneInfo

import httpx
import pandas as pd
import yfinance as yf

NY = ZoneInfo("America/New_York")
MARKET_OPEN_ET = time(9, 30)

TRADIER_SANDBOX_URL = "https://sandbox.tradier.com/v1/markets/quotes"
TRADIER_PRODUCTION_URL = "https://api.tradier.com/v1/markets/quotes"

# Representative basket of large, liquid Nasdaq-100 constituents used to
# approximate market breadth. Not the literal current index membership —
# index composition changes over time and would need its own maintained data
# source to track exactly; this static list is a reasonable, honest proxy.
NASDAQ_BREADTH_BASKET = [
    "AAPL", "MSFT", "NVDA", "GOOGL", "GOOG", "AMZN", "META", "AVGO", "TSLA", "COST",
    "NFLX", "AMD", "PEP", "ADBE", "CSCO", "TMUS", "LIN", "INTC", "INTU", "QCOM",
    "TXN", "AMAT", "CMCSA", "HON", "AMGN", "BKNG", "ISRG", "VRTX", "SBUX", "GILD",
    "MU", "ADI", "LRCX", "PANW", "REGN", "MDLZ", "PYPL", "SNPS", "CDNS", "KLAC",
    "MELI", "CRWD", "MAR", "ORLY", "CSX", "ABNB", "FTNT", "ADP", "PCAR", "NXPI",
    "MNST", "PAYX", "ROP", "DXCM", "AEP", "ODFL", "KDP", "EXC", "CTAS", "CHTR",
    "MRVL", "WDAY", "KHC",
]


class TradierDataError(RuntimeError):
    """Raised when Tradier's quotes feed can't be reached."""


@dataclass
class VixReading:
    """VIX as both a level and a session move. The level alone can't tell a
    calm tape from one that is deteriorating fast: VIX at 18 but +30% on the
    day is a risk-off market that a plain `vix < 22` check reads as benign.
    """
    level: float          # latest VIX print
    session_open: float   # first regular-session bar of the day
    change_pct: float     # % move from session open (positive = fear rising)


@dataclass
class MarketBreadth:
    addq: float          # self-computed Advance-Decline Difference (advancers - decliners)
    advancers: int
    decliners: int
    unchanged: int
    basket_size: int


def fetch_qqq_bars(period: str = "5d", interval: str = "1m") -> pd.DataFrame:
    """1-minute intraday QQQ bars via yfinance. Columns: Open, High, Low, Close, Volume."""
    bars = yf.Ticker("QQQ").history(period=period, interval=interval)
    if bars.empty:
        raise RuntimeError("yfinance returned no QQQ bars — market may be closed or the symbol is unavailable.")
    return bars


def fetch_vix() -> VixReading:
    """CBOE Volatility Index via yfinance, as a level plus its move since the
    session open.

    The session delta comes from the same 1-minute bar series we already
    fetch, so no prior-cycle state has to be persisted to know whether
    volatility is spiking right now.

    The anchor is deliberately the 09:30 ET cash open rather than the first
    bar returned: Cboe publishes VIX through an extended session starting
    around 03:15 ET, and yfinance hands back all of it (~775 bars, not the
    ~390 of regular hours). Anchoring on bar zero would measure the move
    since the middle of the night, burying an opening-bell spike under hours
    of overnight drift.
    """
    bars = yf.Ticker("^VIX").history(period="1d", interval="1m")
    if bars.empty:
        raise RuntimeError("yfinance returned no VIX data.")

    # Pre-market cycles have no regular-hours bar yet; fall back to the full
    # series rather than failing, since the engine doesn't trade then anyway.
    try:
        ny_times = bars.index.tz_convert(NY).time
        regular_hours = bars[ny_times >= MARKET_OPEN_ET]
    except TypeError:  # tz-naive index — shouldn't happen for intraday data
        regular_hours = bars
    session_bars = regular_hours if not regular_hours.empty else bars

    level = float(bars["Close"].iloc[-1])
    session_open = float(session_bars["Open"].iloc[0])
    change_pct = ((level - session_open) / session_open) * 100.0 if session_open else 0.0

    return VixReading(level=level, session_open=session_open, change_pct=change_pct)


async def fetch_market_breadth() -> MarketBreadth:
    """Self-computed Nasdaq breadth: queries NASDAQ_BREADTH_BASKET via Tradier's
    quotes endpoint in one batched call and classifies each name as advancing,
    declining, or unchanged on the day. Requires TRADIER_API_KEY; TRADIER_ENV
    selects sandbox (default) vs production.
    """
    api_key = os.getenv("TRADIER_API_KEY")
    if not api_key:
        raise TradierDataError("TRADIER_API_KEY is not set — market-breadth data cannot be fetched.")

    env = os.getenv("TRADIER_ENV", "sandbox").lower()
    base_url = TRADIER_PRODUCTION_URL if env == "production" else TRADIER_SANDBOX_URL

    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            response = await client.get(
                base_url,
                params={"symbols": ",".join(NASDAQ_BREADTH_BASKET), "greeks": "false"},
                headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise TradierDataError(f"Tradier quotes request failed: {e.response.status_code} {e.response.text}") from e
        except httpx.HTTPError as e:
            raise TradierDataError(f"Tradier quotes request failed: {e}") from e

    payload = response.json()
    quote_data = payload.get("quotes", {}).get("quote")
    if not quote_data:
        raise TradierDataError("Tradier returned no quotes for the Nasdaq breadth basket.")

    quotes = quote_data if isinstance(quote_data, list) else [quote_data]

    advancers = decliners = unchanged = 0
    for q in quotes:
        change = q.get("change")
        if change is None:
            continue
        if change > 0:
            advancers += 1
        elif change < 0:
            decliners += 1
        else:
            unchanged += 1

    return MarketBreadth(
        addq=float(advancers - decliners),
        advancers=advancers,
        decliners=decliners,
        unchanged=unchanged,
        basket_size=len(quotes),
    )
