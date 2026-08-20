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

import logging
import os
from dataclasses import dataclass
from datetime import time
from zoneinfo import ZoneInfo

import httpx
import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

NY = ZoneInfo("America/New_York")
MARKET_OPEN_ET = time(9, 30)

TRADIER_SANDBOX_URL = "https://sandbox.tradier.com/v1/markets/quotes"
TRADIER_PRODUCTION_URL = "https://api.tradier.com/v1/markets/quotes"
TRADIER_SANDBOX_CHAIN_URL = "https://sandbox.tradier.com/v1/markets/options/chains"
TRADIER_PRODUCTION_CHAIN_URL = "https://api.tradier.com/v1/markets/options/chains"

# Nasdaq-100 constituents used to compute market breadth.
#
# Deliberately the Nasdaq-100 rather than the whole exchange. The real $ADDQ
# counts advancers and decliners across all ~3,000 Nasdaq-listed issues and is
# dominated by small caps — but QQQ tracks the Nasdaq-100, so on a day when
# small caps and mega caps diverge, full-market breadth describes stocks the
# traded instrument does not hold. Index breadth is the better-matched signal
# here, not a degraded substitute for it.
#
# Still a static list: index composition changes and tracking it exactly would
# need its own maintained data source. Expect slow drift, and revalidate
# against Tradier when refreshing it.
NASDAQ_BREADTH_BASKET = [
    "AAPL", "MSFT", "NVDA", "GOOGL", "GOOG", "AMZN", "META", "AVGO", "TSLA", "COST",
    "NFLX", "AMD", "PEP", "ADBE", "CSCO", "TMUS", "LIN", "INTC", "INTU", "QCOM",
    "TXN", "AMAT", "CMCSA", "HON", "AMGN", "BKNG", "ISRG", "VRTX", "SBUX", "GILD",
    "MU", "ADI", "LRCX", "PANW", "REGN", "MDLZ", "PYPL", "SNPS", "CDNS", "KLAC",
    "MELI", "CRWD", "MAR", "ORLY", "CSX", "ABNB", "FTNT", "ADP", "PCAR", "NXPI",
    "MNST", "PAYX", "ROP", "DXCM", "AEP", "ODFL", "KDP", "EXC", "CTAS", "CHTR",
    "MRVL", "WDAY", "KHC",
    # Expanded to full Nasdaq-100 coverage. The basket was 63 names, so more
    # than a third of the index QQQ actually tracks was invisible to the
    # breadth signal. Every symbol below was validated against Tradier's
    # quotes endpoint first; ANSS, WBA and EA were dropped from the candidate
    # list because they no longer trade as independent symbols.
    "CEG", "TTD", "DDOG", "TEAM", "CPRT", "FAST", "ROST", "VRSK", "CTSH", "IDXX",
    "FANG", "XEL", "GEHC", "CCEP", "TTWO", "ON", "CDW", "BIIB", "GFS", "MDB",
    "ZS", "ILMN", "WBD", "DLTR", "LULU", "MRNA", "ARM", "SMCI", "APP", "PLTR",
    "MSTR", "AXON", "BKR", "CSGP", "TSCO", "ALGN", "ENPH",
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
class OilReading:
    """WTI crude as a level and an intraday move.

    Read as a rate of change like ^TNX, not a level: crude at $86 says
    nothing on its own, while +3% inside a session is an energy shock feeding
    straight into inflation expectations and the long end.
    """
    level: float          # latest front-month print
    session_open: float   # first regular-session bar of the day
    change_pct: float     # % move from the 09:30 open (positive = crude rising)


@dataclass
class TnxReading:
    """10-year Treasury yield as a level and an intraday move, in basis points.

    The Nasdaq-100 is the longest-duration equity index there is — its
    multiple is a direct function of the discount rate — so a sharp rise in
    real rates is the most reliable macro headwind for QQQ that isn't already
    visible in VIX or breadth. Read as a rate of change, not a level: 10Y at
    4.3% says nothing on its own, while +8bp inside a session is a genuine
    move against a long tech position.
    """
    level: float          # yield in percent, e.g. 4.694
    session_open: float   # first regular-session print
    change_bps: float     # basis points moved since the open (positive = yields rising)


@dataclass
class MarketBreadth:
    addq: float          # self-computed Advance-Decline Difference (advancers - decliners)
    advancers: int
    decliners: int
    unchanged: int
    basket_size: int


# Bar size the indicators are computed on. 5-minute, not 1-minute: on 1-minute
# bars a "20-period" Bollinger band spans 20 MINUTES and a 14-period RSI spans
# 14, which are scalping horizons, while this engine holds positions for tens
# of minutes and targets 30-60% moves. Measured against a month of history,
# running the same gates on 1-minute bars fired 18 signals a day where 5-minute
# bars fired 2.8 — against a market that offers roughly 2.8 tradeable moves.
# The indicators were simply on a faster clock than the strategy.
BAR_INTERVAL = os.getenv("TRADING_BAR_INTERVAL", "5m")


def fetch_qqq_bars(period: str = "5d", interval: str = None) -> pd.DataFrame:
    """Intraday QQQ bars via yfinance at BAR_INTERVAL. Columns: Open, High, Low, Close, Volume."""
    bars = yf.Ticker("QQQ").history(period=period, interval=interval or BAR_INTERVAL)
    if bars.empty:
        raise RuntimeError("yfinance returned no QQQ bars — market may be closed or the symbol is unavailable.")
    return bars


def fetch_qqq_spot() -> float:
    """Freshest QQQ price, from 1-minute bars.

    Separate from the indicator series on purpose: a 5-minute bar's close can
    be five minutes stale, which is fine for a moving average and not fine for
    pricing a spread or choosing a strike.
    """
    bars = yf.Ticker("QQQ").history(period="1d", interval="1m")
    if bars.empty:
        return float(fetch_qqq_bars()["Close"].iloc[-1])
    return float(bars["Close"].iloc[-1])


def fetch_qqq_session_vwap() -> "float | None":
    """Session VWAP for QQQ, priced from yfinance bars but VOLUME-checked.

    yfinance's intraday volume for QQQ is unreliable: measured against
    Tradier's quote for the same session it reported 200.9M against 19.4M,
    and the figure changed between successive calls for identical bars. Price
    agrees to the cent across both feeds, so only the weighting is affected --
    but VWAP is volume-weighted and feeds one of the four entry rules, so a
    skewed weight silently moves an entry condition.

    Tradier's session volume is used to sanity-check the bar volumes. If they
    disagree by more than a factor of two the bar volumes are discarded and
    VWAP falls back to an unweighted typical price, which is less precise but
    is not weighted by a number known to be wrong.
    """
    try:
        bars = yf.Ticker("QQQ").history(period="1d", interval="5m")
        if bars.empty:
            return None
        session = bars[bars.index.tz_convert(NY).time >= MARKET_OPEN_ET]
        if session.empty:
            session = bars
        typical = (session["High"] + session["Low"] + session["Close"]) / 3.0
        bar_vol = float(session["Volume"].sum())

        ref = _tradier_session_volume()
        trust = bar_vol > 0 and (ref is None or 0.5 <= bar_vol / ref <= 2.0)
        if trust:
            return float((typical * session["Volume"]).sum() / bar_vol)

        logger.warning(
            "QQQ bar volume %.0f disagrees with Tradier's %.0f — VWAP falling back to unweighted typical price.",
            bar_vol, ref or 0.0,
        )
        return float(typical.mean())
    except Exception:
        logger.exception("VWAP unavailable.")
        return None


def _tradier_session_volume() -> "float | None":
    """Today's cumulative QQQ volume from Tradier, as a reference."""
    api_key = os.getenv("TRADIER_API_KEY")
    if not api_key:
        return None
    env = os.getenv("TRADIER_ENV", "sandbox").lower()
    base_url = TRADIER_PRODUCTION_URL if env == "production" else TRADIER_SANDBOX_URL
    try:
        r = httpx.get(base_url, params={"symbols": "QQQ", "greeks": "false"},
                      headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
                      timeout=10.0)
        r.raise_for_status()
        q = r.json().get("quotes", {}).get("quote")
        if isinstance(q, list):
            q = q[0] if q else None
        return float(q["volume"]) if q and q.get("volume") is not None else None
    except Exception:
        return None


# Front-month WTI. Configurable because the front month rolls and a data
# vendor outage on one contract should not need a code change.
OIL_SYMBOL = os.getenv("TRADING_OIL_SYMBOL", "CL=F")


def _regular_session_open(bars: pd.DataFrame) -> float:
    """First regular-hours opening print in an intraday bar series.

    yfinance returns the full extended session for both ^VIX and ^TNX — Cboe
    publishes VIX from around 03:15 ET and ^TNX prints from about 08:20 ET —
    so bar zero is an overnight or pre-market quote, not the cash open.
    Anchoring a session move there measures the change since the middle of
    the night and buries an opening-bell move under hours of drift.

    Pre-market cycles have no regular-hours bar yet; fall back to the first
    bar available rather than failing, since the engine doesn't trade then.
    """
    try:
        ny_times = bars.index.tz_convert(NY).time
        regular_hours = bars[ny_times >= MARKET_OPEN_ET]
    except TypeError:  # tz-naive index — shouldn't happen for intraday data
        regular_hours = bars
    session_bars = regular_hours if not regular_hours.empty else bars
    return float(session_bars["Open"].iloc[0])


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

    level = float(bars["Close"].iloc[-1])
    session_open = _regular_session_open(bars)
    change_pct = ((level - session_open) / session_open) * 100.0 if session_open else 0.0

    return VixReading(level=level, session_open=session_open, change_pct=change_pct)


def fetch_oil() -> "OilReading":
    """WTI crude front-month (CL=F) as a level plus its move since the 09:30
    cash open.

    Crude is the macro input QQQ has no other line of sight to. VIX prices
    equity fear, ^TNX prices the discount rate, breadth prices participation
    — none of them see an energy shock until it has already shown up in one
    of those, by which point the move has happened. A crude spike feeds
    inflation expectations, which feeds the long end, which is exactly the
    channel that compresses a long-duration index like the Nasdaq-100.

    CL=F trades close to around the clock — the first bar of the day lands at
    00:00 ET, ~565 bars against the ~390 of a cash session — so the anchor is
    taken from regular hours for the same reason as VIX and ^TNX. Measuring
    from bar zero would report the move since midnight and bury an
    opening-bell spike under overnight drift.
    """
    bars = yf.Ticker(OIL_SYMBOL).history(period="1d", interval="1m")
    if bars.empty:
        raise RuntimeError("yfinance returned no crude oil data.")

    level = float(bars["Close"].iloc[-1])
    session_open = _regular_session_open(bars)
    change_pct = ((level - session_open) / session_open) * 100.0 if session_open else 0.0

    return OilReading(level=level, session_open=session_open, change_pct=change_pct)


def fetch_tnx() -> TnxReading:
    """10-year Treasury yield (CBOE ^TNX) via yfinance, as a level plus its
    move since the 09:30 cash open, in basis points.

    Not sourced from Tradier: confirmed against the quotes endpoint that it
    carries none of TNX, $TNX, ^TNX, TNX.X or US10Y — only ordinary bond ETFs
    like IEF and TLT, which track price rather than yield and would need
    inverting and rescaling to stand in for one. Same situation as $ADDQ and
    $TICKQ.

    yfinance quotes ^TNX directly in percent (4.694 = 4.694%), and returns
    the extended session — the first bar of the day lands around 08:20 ET —
    so the anchor is taken from regular hours for the same reason as VIX.
    """
    bars = yf.Ticker("^TNX").history(period="1d", interval="1m")
    if bars.empty:
        raise RuntimeError("yfinance returned no ^TNX data.")

    level = float(bars["Close"].iloc[-1])
    session_open = _regular_session_open(bars)
    change_bps = (level - session_open) * 100.0

    return TnxReading(level=level, session_open=session_open, change_bps=change_bps)


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


# ---------------------------------------------------------------------------
# Option chain
# ---------------------------------------------------------------------------
#
# Everything the engine has priced until now came out of a normal-CDF model in
# broker.py — entry cost, unrealized P&L, stops, the strike distances, the
# sizing that follows from them. The model is internally consistent, which is
# why it works as a simulator, but it has never once been compared against a
# real quote. Nothing downstream can be more accurate than that assumption.
#
# This module fetches the real thing. It does NOT yet price decisions: the
# first job is to log what the model says next to what the market says, on
# live positions, until there is enough evidence to say which parts of the
# model are wrong and by how much. Greeks come with it, which is what makes
# delta-based strike selection possible later.


@dataclass(frozen=True)
class OptionQuote:
    """One leg, as the market actually quotes it."""
    symbol: str
    strike: float
    option_type: str          # 'call' or 'put'
    bid: float
    ask: float
    delta: "float | None"
    gamma: "float | None"
    theta: "float | None"
    iv: "float | None"
    volume: int
    open_interest: int

    @property
    def mid(self) -> float:
        """Midpoint, or whichever side exists if the book is one-sided.

        A 0DTE strike far out of the money routinely quotes 0.00 bid, and
        averaging that with the ask understates what closing it costs.
        """
        if self.bid > 0 and self.ask > 0:
            return round((self.bid + self.ask) / 2.0, 4)
        return round(self.ask or self.bid, 4)


def today_expiry(now=None) -> str:
    """Today's contract expiration, YYYY-MM-DD in market time.

    QQQ lists daily expirations, and the engine only ever trades the one that
    expires today — so this is the expiry, not a choice.
    """
    from datetime import datetime
    now = now or datetime.now(NY)
    return now.strftime("%Y-%m-%d")


def fetch_option_chain(expiration: "str | None" = None, symbol: str = "QQQ") -> dict:
    """The full chain for one expiration, keyed by (option_type, strike).

    Returns an empty dict rather than raising when the chain is unavailable.
    Every caller so far is observational — a chain that fails to load must
    degrade to the model quietly, not stop a cycle that was managing a live
    position.
    """
    api_key = os.getenv("TRADIER_API_KEY")
    if not api_key:
        logger.warning("TRADIER_API_KEY is not set — option chain unavailable.")
        return {}

    env = os.getenv("TRADIER_ENV", "sandbox").lower()
    base_url = TRADIER_PRODUCTION_CHAIN_URL if env == "production" else TRADIER_SANDBOX_CHAIN_URL
    expiration = expiration or today_expiry()

    try:
        r = httpx.get(
            base_url,
            params={"symbol": symbol, "expiration": expiration, "greeks": "true"},
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
            timeout=10.0,
        )
        r.raise_for_status()
        payload = (r.json() or {}).get("options") or {}
        rows = payload.get("option") or []
    except httpx.HTTPError as e:
        logger.warning("Option chain fetch failed (%s) — falling back to the model.", e)
        return {}
    except ValueError as e:
        logger.warning("Option chain returned unparseable JSON (%s).", e)
        return {}

    if isinstance(rows, dict):        # Tradier collapses a single result
        rows = [rows]

    chain = {}
    for row in rows:
        try:
            greeks = row.get("greeks") or {}
            q = OptionQuote(
                symbol=row.get("symbol", ""),
                strike=float(row["strike"]),
                option_type=row.get("option_type", ""),
                bid=float(row.get("bid") or 0.0),
                ask=float(row.get("ask") or 0.0),
                delta=_maybe_float(greeks.get("delta")),
                gamma=_maybe_float(greeks.get("gamma")),
                theta=_maybe_float(greeks.get("theta")),
                iv=_maybe_float(greeks.get("mid_iv")),
                volume=int(row.get("volume") or 0),
                open_interest=int(row.get("open_interest") or 0),
            )
        except (KeyError, TypeError, ValueError):
            continue
        chain[(q.option_type, q.strike)] = q
    if not chain:
        logger.warning("Option chain for %s %s came back empty.", symbol, expiration)
    return chain


def _maybe_float(v) -> "float | None":
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def chain_vertical(chain: dict, option_type: str, buy_strike: float,
                   sell_strike: float) -> "dict | None":
    """Market price of a vertical: long `buy_strike`, short `sell_strike`.

    Mirrors broker.estimate_spread_value's argument order, so the model and
    the market can be compared leg for leg. Both credit and debit structures
    fit: a credit spread's cost to close is the same vertical with the short
    strike as the long leg, which is exactly how estimate_credit_value
    delegates.

    bid/ask are the NATURAL prices — what you would actually receive selling
    the spread and pay buying it, each crossing the wrong side of both legs.
    That gap is the number the model's flat fill assumption cannot see.
    """
    long_leg = chain.get((option_type, float(buy_strike)))
    short_leg = chain.get((option_type, float(sell_strike)))
    if long_leg is None or short_leg is None:
        return None
    return {
        "bid": round(long_leg.bid - short_leg.ask, 4),
        "ask": round(long_leg.ask - short_leg.bid, 4),
        "mid": round(long_leg.mid - short_leg.mid, 4),
        "long_leg": long_leg,
        "short_leg": short_leg,
    }


def strike_for_delta(chain: dict, option_type: str, target_delta: float) -> "float | None":
    """The listed strike whose delta sits closest to `target_delta`.

    Puts quote a negative delta; the comparison is on magnitude so a caller
    can ask for 0.60 without knowing which side it is on. Nothing calls this
    for placement yet — it exists so the 60-delta long / 20-delta short shape
    can be measured against the deep-ITM placement on real greeks rather than
    on a model's opinion of what a 60 delta is.
    """
    best, best_gap = None, None
    for (kind, strike), q in chain.items():
        if kind != option_type or q.delta is None:
            continue
        gap = abs(abs(q.delta) - abs(target_delta))
        if best_gap is None or gap < best_gap:
            best, best_gap = strike, gap
    return best


def log_price_divergence(option_type: str, buy_strike: float, sell_strike: float,
                         model_value: float, label: str, chain: "dict | None" = None) -> "dict | None":
    """Record what the model priced against what the chain quotes.

    Observational only, and deliberately so. The model sizes positions, sets
    stops and decides exits; swapping it for live quotes changes every one of
    those at once, on a system that is currently making money. This logs the
    gap first so the swap can be argued from evidence — and so a chain that
    is thin, stale or one-sided shows up as a bad data source before it is
    trusted with a decision.

    Returns the market quote, or None when the chain has nothing for these
    strikes.
    """
    chain = fetch_option_chain() if chain is None else chain
    if not chain:
        return None
    market = chain_vertical(chain, option_type, buy_strike, sell_strike)
    if market is None:
        logger.info("Chain has no %s %.0f/%.0f — model-only for %s.",
                    option_type, buy_strike, sell_strike, label)
        return None

    mid = market["mid"]
    gap = mid - model_value
    pct = (gap / model_value * 100.0) if model_value else 0.0
    long_leg, short_leg = market["long_leg"], market["short_leg"]
    logger.info(
        "Chain vs model [%s] %s %.0f/%.0f: model %.3f, market mid %.3f (%+.3f, %+.1f%%), "
        "natural %.3f/%.3f — legs %.0fΔ %.3f / %.0fΔ %.3f, OI %d/%d",
        label, option_type, buy_strike, sell_strike, model_value, mid, gap, pct,
        market["bid"], market["ask"],
        (long_leg.delta or 0) * 100, long_leg.mid,
        (short_leg.delta or 0) * 100, short_leg.mid,
        long_leg.open_interest, short_leg.open_interest,
    )
    return market
