"""Where the screener's option quotes come from (2026-09-21).

The board returned zero candidates for all twelve names with no warning.
Yahoo's chains had bid and ask of 0.00 on nearly every strike -- AAPL 09-25:
0 of 68 priced -- so usable() rightly rejected them all. Tradier's chain for
the same expiry was priced on 70 of 79. evaluate() now takes the broker's
quotes first and falls back to Yahoo, and rank() names the reason when a
symbol contributes nothing.

Everything here is offline: yfinance and the Tradier fetcher are both
replaced by fakes.
"""

import numpy as np
import pandas as pd
import pytest

from trading_engine.data_feed import OptionQuote


def _history():
    rng = np.random.default_rng(3)
    n = 200
    close = 100.0 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    idx = pd.bdate_range(end="2026-09-21", periods=n)
    return pd.DataFrame({"Open": close, "High": close * 1.01,
                         "Low": close * 0.99, "Close": close}, index=idx)


def _yahoo_chain(spot: float, priced: bool):
    """Yahoo's shape. `priced=False` is what it returns now: 0.00/0.00 with a
    placeholder IV, on every strike."""
    def side(call: bool):
        rows = []
        for f in (0.94, 0.96, 0.98, 1.0, 1.02, 1.04, 1.06):
            k = round(spot * f)
            intrinsic = max(spot - k, 0.0) if call else max(k - spot, 0.0)
            mid = intrinsic + 2.0
            rows.append({"strike": float(k),
                         "bid": (mid - 0.10) if priced else 0.0,
                         "ask": (mid + 0.10) if priced else 0.0,
                         "openInterest": 500,
                         "impliedVolatility": 0.45 if priced else 0.00001})
        return pd.DataFrame(rows)

    class _Chain:
        calls = side(True)
        puts = side(False)

    return _Chain()


def _tradier_chain(spot: float) -> dict:
    ch = {}
    for f in (0.94, 0.96, 0.98, 1.0, 1.02, 1.04, 1.06):
        k = float(round(spot * f))
        for t in ("call", "put"):
            intrinsic = max(spot - k, 0.0) if t == "call" else max(k - spot, 0.0)
            mid = intrinsic + 2.0
            ch[(t, k)] = OptionQuote(symbol=f"FAKE{t}{k}", strike=k, option_type=t,
                                     bid=mid - 0.10, ask=mid + 0.10, delta=0.5,
                                     gamma=None, theta=None, iv=0.45,
                                     volume=100, open_interest=500)
    return ch


class _Ticker:
    priced_yahoo = False

    def __init__(self, sym):
        self._h = _history()
        self.spot = float(self._h["Close"].iloc[-1])

    def history(self, period, interval):
        return self._h

    @property
    def options(self):
        return ("2026-09-25",)

    def option_chain(self, exp):
        return _yahoo_chain(self.spot, self.priced_yahoo)


@pytest.fixture
def wp(monkeypatch):
    from trading_engine import screener  # noqa: F401  puts scripts/ on sys.path
    import weekly_pick as wp
    import trading_engine.data_feed as df

    monkeypatch.setattr(wp.yf, "Ticker", _Ticker)
    monkeypatch.setattr(wp, "news_verdict", lambda sym: None)
    monkeypatch.setattr(wp, "flow_read", lambda sym: {})
    monkeypatch.setattr(wp, "MC_PATHS", 500)
    spot = _Ticker("FAKE").spot
    monkeypatch.setattr(df, "fetch_option_chain",
                        lambda exp, sym="QQQ": _tradier_chain(spot))
    return wp


def test_broker_quotes_are_used_when_yahoo_is_blank(wp):
    rows, meta = wp.evaluate("FAKE", "call", "debit", "2026-09-25")
    assert meta["quotes"] == "tradier"
    assert meta["strikes"] == 7 and meta["quoted"] == 7
    assert rows, "priced broker quotes must yield candidates"


def test_yahoo_is_the_fallback_when_the_broker_has_nothing(wp, monkeypatch):
    import trading_engine.data_feed as df

    monkeypatch.setattr(df, "fetch_option_chain", lambda exp, sym="QQQ": {})
    monkeypatch.setattr(_Ticker, "priced_yahoo", True)
    rows, meta = wp.evaluate("FAKE", "put", "debit", "2026-09-25")
    assert meta["quotes"] == "yahoo"
    assert rows


def test_rank_explains_a_name_with_no_candidates(wp, monkeypatch):
    """Broker empty AND Yahoo blank: the old silent zero, now a warning
    that names the filter."""
    import trading_engine.data_feed as df

    monkeypatch.setattr(df, "fetch_option_chain", lambda exp, sym="QQQ": {})
    out = wp.rank(["FAKE"], "call", by="edge", expiry="2026-09-25")
    assert out["rows"] == []
    assert len(out["warnings"]) == 1
    w = out["warnings"][0]
    assert w.startswith("FAKE: no debit call spread priced")
    assert "0 of 7 yahoo strikes" in w


def test_rank_is_quiet_when_a_name_has_candidates(wp):
    out = wp.rank(["FAKE"], "call", by="edge", expiry="2026-09-25")
    assert out["rows"] and out["warnings"] == []
    assert out["meta"][0]["quotes"] == "tradier"
