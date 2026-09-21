"""The screener board's 500 before the open (2026-09-21).

GET /trading/screener/verticals died at 09:2x ET on every request with
"Out of range float values are not JSON compliant". Before the open yfinance
appended today's row to the daily history with every price NaN; spot read as
NaN, ATR followed, and every EV, probability and `itm` in every row was NaN.
The endpoint had no defence at either end. Now it has two, and each is tested
on its own:

  * evaluate() drops bars with no close before doing any maths;
  * the route passes its payload through _json_safe, so a NaN that gets
    through anyway becomes null rather than a 500 with no body.
"""

import math

import numpy as np
import pandas as pd
import pytest


# ---------------------------------------------------------------------------
# _json_safe
# ---------------------------------------------------------------------------

def test_json_safe_nulls_nan_and_inf_recursively():
    from routes.trading_router import _json_safe

    out = _json_safe({
        "a": float("nan"), "b": float("inf"), "c": -float("inf"), "d": 1.5,
        "rows": [{"x": float("nan"), "y": 2}, (float("inf"), "s")],
        "meta": {"iv": float("nan"), "n": 3, "label": "ok", "none": None},
    })
    assert out["a"] is None and out["b"] is None and out["c"] is None
    assert out["d"] == 1.5
    assert out["rows"][0] == {"x": None, "y": 2}
    assert out["rows"][1] == [None, "s"]
    assert out["meta"] == {"iv": None, "n": 3, "label": "ok", "none": None}


def test_json_safe_output_encodes():
    import json

    from routes.trading_router import _json_safe

    payload = {"ev": float("nan"), "rows": [{"p": float("inf")}]}
    # FastAPI's JSONResponse encodes with allow_nan=False; plain json.dumps
    # would happily write NaN, which is not JSON either.
    with pytest.raises(ValueError):
        json.dumps(payload, allow_nan=False)             # the failure the board saw
    json.dumps(_json_safe(payload), allow_nan=False)     # and what it gets now


# ---------------------------------------------------------------------------
# evaluate() with a pre-open history: last row all NaN
# ---------------------------------------------------------------------------

class _Chain:
    def __init__(self, calls, puts):
        self.calls, self.puts = calls, puts


class _FakeTicker:
    """200 sessions of a 100-dollar stock, then today's empty pre-open row,
    and a small liquid chain around the money."""

    def __init__(self, sym):
        rng = np.random.default_rng(1)
        n = 200
        close = 100.0 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
        idx = pd.bdate_range(end="2026-09-18", periods=n)
        h = pd.DataFrame({"Open": close, "High": close * 1.01,
                          "Low": close * 0.99, "Close": close}, index=idx)
        today = pd.DataFrame({"Open": [np.nan], "High": [np.nan],
                              "Low": [np.nan], "Close": [np.nan]},
                             index=pd.DatetimeIndex(["2026-09-21"]))
        self._h = pd.concat([h, today])
        self._spot = float(close[-1])

    def history(self, period, interval):
        return self._h

    @property
    def options(self):
        return ("2026-09-25",)

    def option_chain(self, exp):
        s = self._spot
        strikes = [round(s * f) for f in (0.94, 0.96, 0.98, 1.0, 1.02, 1.04, 1.06)]
        rows = []
        for k in strikes:
            intrinsic = max(s - k, 0.0)
            mid = intrinsic + 2.0
            rows.append({"strike": float(k), "bid": mid - 0.10, "ask": mid + 0.10,
                         "openInterest": 500, "impliedVolatility": 0.45})
        calls = pd.DataFrame(rows)
        prow = []
        for k in strikes:
            intrinsic = max(k - s, 0.0)
            mid = intrinsic + 2.0
            prow.append({"strike": float(k), "bid": mid - 0.10, "ask": mid + 0.10,
                         "openInterest": 500, "impliedVolatility": 0.45})
        return _Chain(calls, pd.DataFrame(prow))


def _finite_everywhere(obj) -> bool:
    if isinstance(obj, float):
        return math.isfinite(obj)
    if isinstance(obj, dict):
        return all(_finite_everywhere(v) for v in obj.values())
    if isinstance(obj, (list, tuple)):
        return all(_finite_everywhere(v) for v in obj)
    return True


@pytest.fixture
def weekly_pick(monkeypatch):
    from trading_engine import screener  # puts scripts/ on sys.path
    import weekly_pick as wp

    monkeypatch.setattr(wp.yf, "Ticker", _FakeTicker)
    monkeypatch.setattr(wp, "news_verdict", lambda sym: None)
    monkeypatch.setattr(wp, "flow_read", lambda sym: {})
    monkeypatch.setattr(wp, "MC_PATHS", 500)
    return wp


def test_evaluate_ignores_the_empty_preopen_row(weekly_pick):
    rows, meta = weekly_pick.evaluate("FAKE", "call", "debit", "2026-09-25")
    assert meta is not None
    assert meta["spot"] > 0 and meta["atr"] > 0
    assert rows, "a liquid chain around the money should yield candidates"
    assert _finite_everywhere(meta)
    for r in rows:
        assert _finite_everywhere({k: v for k, v in r.items() if k != "flow"}), r


def test_rank_result_is_json_encodable(weekly_pick):
    import json

    from routes.trading_router import _json_safe

    out = weekly_pick.rank(["FAKE"], "put", by="edge", top=5, expiry="2026-09-25")
    assert out["warnings"] == []
    json.dumps(_json_safe(out))


def test_evaluate_reports_rather_than_returns_nan(weekly_pick, monkeypatch):
    """If every row were empty the name is skipped with a reason, which
    rank() collects into warnings -- not a page of NaN."""
    class _AllNaN(_FakeTicker):
        def history(self, period, interval):
            h = self._h.copy()
            h[["Open", "High", "Low", "Close"]] = np.nan
            return h

    monkeypatch.setattr(weekly_pick.yf, "Ticker", _AllNaN)
    rows, meta = weekly_pick.evaluate("FAKE", "call", "debit", "2026-09-25")
    assert rows == [] and meta is None          # too short once the NaN rows go
    out = weekly_pick.rank(["FAKE"], "call", expiry="2026-09-25")
    assert out["rows"] == [] and out["meta"] == []
