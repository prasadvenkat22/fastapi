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
SESSION VWAP. It is a session-scoped intraday measure and a weekly position
spans five sessions and five VWAPs, so there is no single value to record and
no meaning in the one from Friday afternoon. It belongs to the 0DTE engine
and does not transfer.

  [2026-09-06: that argument was stated as if it ruled out VWAP entirely, and
  it does not. It rules out the SESSION form only. A VWAP ANCHORED to Monday's
  open is one value across the whole week, does not reset, and is the level
  the week's flow actually transacted at -- exactly the reference a five-day
  hold wants on day four. "There is no single value to record" is precisely
  wrong for the anchored form. sig_vwap_week and sig_vwap_side below.]

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
from datetime import timedelta

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

    # ATR14, not mean(High-Low). True Range takes the GAP into account --
    # max(H-L, |H-prevC|, |L-prevC|) -- and the gap is exactly what a news
    # catalyst produces. Measured across the tracked names on 2026-09-06,
    # High-Low understates the real daily move by 4% (DELL) to 29% (MRVL),
    # with QQQ at 23%; section 102 sized every width in the book on the
    # understated version. SNDK's 2026-09-04 session is the case in one line:
    # High-Low 159.00, True Range 185.01, ATR14 109.83 -- a 1.68x day that
    # the H-L reading would have called 1.45x.
    atr14 = atr_pct = None
    try:
        highs = bars["High"].astype(float)
        lows = bars["Low"].astype(float)
        prev = closes.shift(1)
        tr = (highs - lows).combine((highs - prev).abs(), max).combine(
            (lows - prev).abs(), max)
        if len(tr.dropna()) >= 14:
            atr14 = round(float(tr.rolling(14).mean().iloc[-1]), 4)
            atr_pct = round(atr14 / close * 100.0, 3) if close else None
    except Exception:
        pass

    # WEEKLY-ANCHORED VWAP. Anchored to the most recent Monday, so it is ONE
    # value spanning the same sessions the position spans, and it does not
    # reset under the trade the way a session VWAP does.
    #
    # Approximated from DAILY bars: typical price (H+L+C)/3 weighted by daily
    # volume. That is coarser than a true intraday tick VWAP, and the column
    # should be read as "roughly where the week's volume transacted", not as
    # an exact figure. Tradier's timesales already returns a per-bar vwap --
    # data_feed._tradier_session_vwap consumes it -- so a precise version is
    # available for the live path if this one ever earns its place.
    vwap_week = vwap_side = None
    try:
        idx = list(bars.index)
        last = idx[-1]
        last_date = last.date() if hasattr(last, "date") else last
        # Monday of the week containing the latest session.
        anchor = last_date - timedelta(days=last_date.weekday())
        num = den = 0.0
        for i, ts in enumerate(idx):
            d = ts.date() if hasattr(ts, "date") else ts
            if d < anchor:
                continue
            vol = float(bars["Volume"].iloc[i] or 0.0)
            if vol <= 0:
                continue
            typical = (float(bars["High"].iloc[i]) + float(bars["Low"].iloc[i])
                       + float(bars["Close"].iloc[i])) / 3.0
            num += typical * vol
            den += vol
        if den > 0:
            vwap_week = round(num / den, 4)
            # A band, not a knife-edge: within 0.25 ATR of the level is AT, so
            # the label does not flip on noise the position cannot feel.
            tol = (atr14 or 0.0) * 0.25
            if close > vwap_week + tol:
                vwap_side = "ABOVE"
            elif close < vwap_week - tol:
                vwap_side = "BELOW"
            else:
                vwap_side = "AT"
    except Exception:
        pass

    move_5d = None
    if len(closes) >= 6:
        prior = float(closes.iloc[-6])
        if prior:
            move_5d = round((close - prior) / prior * 100.0, 3)

    return {
        "close": round(close, 4),
        "atr14": atr14,
        "vwap_week": vwap_week,
        "vwap_side": vwap_side,
        "atr_pct": atr_pct,
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


_EARNINGS_CACHE: dict = {}


def earnings_between(symbol: str, start, end) -> "object | None":
    """The symbol's next earnings date if it falls inside [start, end].

    A weekly credit spread held over an earnings print is a different trade
    from the one a 0.12-delta strike was chosen for. The delta prices an
    ordinary five days; an earnings gap is the tail that delta is not
    describing, and the position cannot be managed while it happens.

    Returns None for an ETF, for a symbol with no published date, or when
    yfinance fails -- and the CALLER must treat None as "unknown", not as
    "safe". A guard that silently passes on a data outage is worse than no
    guard, so the live path refuses on an exception rather than proceeding.

    Cached per symbol per day: this runs once a week in practice but sits in
    the same cycle as everything else.
    """
    key = (symbol.upper(), start)
    if key in _EARNINGS_CACHE:
        return _EARNINGS_CACHE[key]
    try:
        rows = yf.Ticker(symbol).get_earnings_dates(limit=8)
    except Exception:
        logger.exception("Earnings lookup failed for %s", symbol)
        raise
    hit = None
    if rows is not None and len(rows):
        for ts in rows.index:
            d = ts.date()
            if start <= d <= end:
                hit = d
                break
    _EARNINGS_CACHE[key] = hit
    return hit


def _news(symbol: str) -> dict:
    """Headline count and latest headline for the name, or empty on failure.

    Imported lazily so a signals read cannot be broken by the news module or
    its database driver being unavailable -- these columns are notes.
    """
    try:
        from .symbol_news import news_signals
        return news_signals(symbol)
    except Exception:
        return {}


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
        "sig_atr14": sym.get("atr14"),
        "sig_vwap_week": sym.get("vwap_week"),
        "sig_vwap_side": sym.get("vwap_side"),
        "sig_atr_pct": sym.get("atr_pct"),
        # Cushion in the name's OWN daily moves -- section 102's test, done
        # with True Range. Under 1 is a coin flip whatever the position cost;
        # over 2 survives a shock. Null when no strike is supplied, because
        # the caller knows the strike and this function does not.
        **_news(symbol),
        "sig_index_symbol": INDEX_SYMBOL,
        "sig_index_trend": idx.get("trend"),
        "sig_index_bb_zone": idx.get("bb_zone"),
        "sig_index_rsi14": idx.get("rsi14"),
        "sig_index_move_5d_pct": idx.get("move_5d_pct"),
    }
