"""Live market data for the trading graph.

Price/VIX bars come from yfinance (free, no key required). Market-breadth
internals ($ADDQ / $TICKQ) come from Tradier's market-data quotes endpoint —
these are niche symbols and not every data vendor carries them under this
exact naming convention, so the fetch raises a clear, specific error rather
than silently returning zeros if Tradier's feed doesn't resolve them.
"""

import os
from dataclasses import dataclass
from typing import Optional

import httpx
import pandas as pd
import yfinance as yf

TRADIER_SANDBOX_URL = "https://sandbox.tradier.com/v1/markets/quotes"
TRADIER_PRODUCTION_URL = "https://api.tradier.com/v1/markets/quotes"


class TradierDataError(RuntimeError):
    """Raised when Tradier's quotes feed can't be reached or doesn't carry a requested symbol."""


@dataclass
class MarketBreadth:
    addq: float   # Nasdaq Advance-Decline Difference
    tickq: float  # Nasdaq Net Tick Index


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
    """Nasdaq Advance-Decline Difference ($ADDQ) and Net Tick Index ($TICKQ) via Tradier.

    Requires TRADIER_API_KEY. TRADIER_ENV selects sandbox (default) vs production.
    Symbol names are configurable (TRADIER_ADDQ_SYMBOL / TRADIER_TICKQ_SYMBOL) —
    Tradier may not carry these exact tickers depending on your account's data
    entitlements; adjust the env vars if the default names don't resolve.
    """
    api_key = os.getenv("TRADIER_API_KEY")
    if not api_key:
        raise TradierDataError("TRADIER_API_KEY is not set — market-breadth data cannot be fetched.")

    env = os.getenv("TRADIER_ENV", "sandbox").lower()
    base_url = TRADIER_PRODUCTION_URL if env == "production" else TRADIER_SANDBOX_URL

    addq_symbol = os.getenv("TRADIER_ADDQ_SYMBOL", "$ADDQ")
    tickq_symbol = os.getenv("TRADIER_TICKQ_SYMBOL", "$TICKQ")

    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            response = await client.get(
                base_url,
                params={"symbols": f"{addq_symbol},{tickq_symbol}", "greeks": "false"},
                headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise TradierDataError(f"Tradier quotes request failed: {e.response.status_code} {e.response.text}") from e
        except httpx.HTTPError as e:
            raise TradierDataError(f"Tradier quotes request failed: {e}") from e

    payload = response.json()
    quote_data = payload.get("quotes", {}).get("quote")
    if quote_data is None:
        raise TradierDataError(f"Tradier returned no quotes for {addq_symbol}/{tickq_symbol} — check symbol availability for your account.")

    quotes = quote_data if isinstance(quote_data, list) else [quote_data]
    by_symbol = {q.get("symbol"): q for q in quotes}

    addq_quote = by_symbol.get(addq_symbol)
    tickq_quote = by_symbol.get(tickq_symbol)

    if not addq_quote or addq_quote.get("last") is None:
        raise TradierDataError(f"Tradier did not return a resolvable value for '{addq_symbol}'. This symbol may not be carried by your data feed.")
    if not tickq_quote or tickq_quote.get("last") is None:
        raise TradierDataError(f"Tradier did not return a resolvable value for '{tickq_symbol}'. This symbol may not be carried by your data feed.")

    return MarketBreadth(addq=float(addq_quote["last"]), tickq=float(tickq_quote["last"]))
