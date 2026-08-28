"""Market state at the moment a weekly shadow row is opened.

Why this exists
---------------
The proposal is to gate the weekly single-name book on the same kind of
reading the 0DTE engine uses -- index direction, moving averages, Bollinger
position -- plus realised-against-implied volatility. None of that can be
judged today, because weekly_shadow rows record strikes, deltas and credits
and nothing at all about the market they were opened into. There is no
historical option-chain feed, so the gate cannot be backtested; the only
honest evidence is forward evidence, which means recording the inputs now and
asking the question once eight or ten Fridays exist.

Recording it costs nothing and commits to nothing. Nothing here gates an
entry: weekly_shadow still opens every structure on every symbol, exactly as
before, and these columns are notes beside the row.

What is deliberately NOT here
-----------------------------
VWAP. It is a session-scoped intraday measure and a weekly position spans
five sessions and five VWAPs, so there is no single value to record and no
meaning in the one from Friday afternoon. It belongs to the 0DTE engine and
does not transfer.

Everything is computed on DAILY bars, not the 5-minute bars data_feed serves
the 0DTE engine. A 20-period Bollinger band on 5-minute bars describes the
last hour and a half; on a five-day hold that is noise. The indicator names
are the same and the timescale is not, which is the whole point.

Both the LABEL and the NUMBER are stored for every reading. The label is what
a gate would read; the number is what lets a different threshold be tested in
six months without waiting another ten Fridays to re-collect the data.
"""
import logging
import math

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

# The index every single name is read against. "Overall market moves" needs a
# concrete series, and QQQ is the one the rest of this engine already trades
# and understands.
INDEX_SYMBOL = "QQQ"

# Trading days in a year, for annualising realised volatility so it is on the
# same footing as an implied vol quoted by the chain.
TRADING_DAYS = 252


def _daily(symbol: str, period: str = "6mo") -> "pd.DataFrame | None":
    try:
        bars = yf.Ticker(symbol).history(period=period, interval="1d")
    except Exception:
        logger.exception("Daily bars unavailable for %s", symbol)
        return None
    if bars is None or len(bars) < 55:
        # 55 because the longest average here is 50 periods; anything shorter
        # would report an SMA computed from a partial window as though it were
        # the real one.
        logger.warning("Only %d daily bars for %s - signals skipped.",
                       0 if bars is None else len(bars), symbol)
        return None
    return bars


def _rsi(closes: pd.Series, period: int = 14) -> "float | None":
    """Wilder RSI, matching what nodes.py computes for the 0DTE engine."""
    if len(closes) < period + 1:
        return None
    delta = closes.diff()
    gain = delta.clip(lower=0.0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0.0)).ewm(alpha=1 / period, adjust=False).mean()
    last_loss = float(loss.iloc[-1])
    if last_loss == 0:
        return 100.0
    rs = float(gain.iloc[-1]) / last_loss
    return round(100.0 - (100.0 / (1.0 + rs)), 2)


def _realised_vol(closes: pd.Series, days: int = 20) -> "float | None":
    """Annualised close-to-close realised volatility over the last `days`.

    Close-to-close rather than a range estimator: it is what an implied vol is
    quoted against, and that comparison is the entire point of the column.
    """
    if len(closes) < days + 1:
        return None
    rets = (closes / closes.shift(1)).apply(lambda x: math.log(x) if x > 0 else 0.0)
    sd = float(rets.tail(days).std())
    return round(sd * math.sqrt(TRADING_DAYS), 4)


def _trend_label(close: float, ema20: float, sma50: float) -> str:
    if close > ema20 and close > sma50:
        return "ABOVE_BOTH"
    if close < ema20 and close < sma50:
        return "BELOW_BOTH"
    return "MIXED"


_CACHE: dict = {}


def read(symbol: str) -> dict:
    """One symbol's daily-bar state.

    The realised-to-implied ratio is NOT here: it needs a variant's own short
    leg, and the three variants on a symbol share these bars. entry_signals
    combines the two.

    Returns {} when the bars are unavailable, so a caller writes nulls rather
    than failing an observation that is only taking notes.
    """
    bars = _daily(symbol)
    if bars is None:
        return {}
    closes = bars["Close"].astype(float)
    close = float(closes.iloc[-1])
    ema9 = float(closes.ewm(span=9, adjust=False).mean().iloc[-1])
    ema20 = float(closes.ewm(span=20, adjust=False).mean().iloc[-1])
    sma20 = float(closes.rolling(20).mean().iloc[-1])
    sma50 = float(closes.rolling(50).mean().iloc[-1])
    sd20 = float(closes.rolling(20).std().iloc[-1])

    upper, lower = sma20 + 2 * sd20, sma20 - 2 * sd20
    if close >= upper:
        bb_zone = "UPPER_BAND"
    elif close <= lower:
        bb_zone = "LOWER_BAND"
    else:
        bb_zone = "NORMAL"

    rv20 = _realised_vol(closes, 20)

    move_5d = None
    if len(closes) >= 6:
        prior = float(closes.iloc[-6])
        if prior:
            move_5d = round((close - prior) / prior * 100.0, 3)

    return {
        "close": round(close, 4),
        "ema9": round(ema9, 4),
        "ema20": round(ema20, 4),
        "sma20": round(sma20, 4),
        "sma50": round(sma50, 4),
        "bb_sd": round(sd20, 4),
        "bb_zone": bb_zone,
        "trend": _trend_label(close, ema20, sma50),
        "ema_cross": "ABOVE" if ema9 > ema20 else "BELOW",
        "rsi14": _rsi(closes),
        "rv20": rv20,
        "move_5d_pct": move_5d,
    }


def _cached(symbol: str, day) -> dict:
    """One daily-bar fetch per symbol per day.

    _maybe_open runs per symbol and each symbol carries three variants, so an
    uncached read would fetch the same bars three times for the name and
    thirty-three times for the index on a twelve-symbol Friday.
    """
    key = (symbol.upper(), day)
    if key not in _CACHE:
        # Bounded so a long-lived process cannot accumulate a reading per
        # symbol per day forever. Cleared wholesale rather than evicted by age:
        # the cache is only ever meant to survive one Friday's pass.
        if len(_CACHE) > 64:
            _CACHE.clear()
        _CACHE[key] = read(symbol)
    return _CACHE[key]


def entry_signals(symbol: str, short_iv: "float | None" = None,
                  day=None) -> dict:
    """Columns for one weekly_shadow row: the name's state and the index's.

    short_iv is the variant's own short-leg implied vol, so the three variants
    on one symbol share every daily-bar reading and differ only in the ratio.
    That is correct: the call and the put are quoted at different vols, and a
    single ratio for the symbol would hide exactly the skew the wings are
    recorded separately to expose.
    """
    sym = _cached(symbol, day)
    idx = sym if symbol.upper() == INDEX_SYMBOL else _cached(INDEX_SYMBOL, day)
    if not sym and not idx:
        return {}
    rv20 = sym.get("rv20")
    # The ratio the whole single-name question turns on. Above 1.0 the
    # underlying has been realising MORE than the market charges, which is the
    # wrong side of the variance risk premium and the condition under which no
    # technical gate can rescue a short-premium structure.
    rv_iv = round(rv20 / short_iv, 4) if (rv20 and short_iv) else None
    return {
        "sig_trend": sym.get("trend"),
        "sig_ema_cross": sym.get("ema_cross"),
        "sig_bb_zone": sym.get("bb_zone"),
        "sig_bb_sd": sym.get("bb_sd"),
        "sig_rsi14": sym.get("rsi14"),
        "sig_sma20": sym.get("sma20"),
        "sig_sma50": sym.get("sma50"),
        "sig_ema20": sym.get("ema20"),
        "sig_move_5d_pct": sym.get("move_5d_pct"),
        "sig_rv20": rv20,
        "sig_rv_iv_ratio": rv_iv,
        "sig_index_symbol": INDEX_SYMBOL,
        "sig_index_trend": idx.get("trend"),
        "sig_index_bb_zone": idx.get("bb_zone"),
        "sig_index_rsi14": idx.get("rsi14"),
        "sig_index_move_5d_pct": idx.get("move_5d_pct"),
    }
