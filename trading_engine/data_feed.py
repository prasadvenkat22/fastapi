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

import httpx
import pandas as pd
import yfinance as yf

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


def fetch_vix() -> float:
    """Latest CBOE Volatility Index value via yfinance."""
    bars = yf.Ticker("^VIX").history(period="1d", interval="1m")
    if bars.empty:
        raise RuntimeError("yfinance returned no VIX data.")
    return float(bars["Close"].iloc[-1])


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
