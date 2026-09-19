"""The options-flow read on synthetic chains. Section 197."""
import importlib

import pytest


@pytest.fixture
def of(monkeypatch):
    monkeypatch.setenv("TRADING_OPTFLOW_CP_RATIO", "2.0")
    monkeypatch.setenv("TRADING_OPTFLOW_TURNOVER", "0.5")
    monkeypatch.setenv("TRADING_OPTFLOW_MIN_VOLUME", "500")
    import trading_engine.options_flow as m
    return importlib.reload(m)


def chain(calls, puts):
    """calls/puts: list of (strike, volume, oi, bid, ask) -> chain dict keyed (type, strike)."""
    out = {}
    for typ, rows in (("call", calls), ("put", puts)):
        for k, v, oi, b, a in rows:
            out[(typ, k)] = {"option_type": typ, "strike": k, "volume": v, "open_interest": oi,
                             "bid": b, "ask": a}
    return out


def test_call_block_reads_bullish(of):
    c = chain(calls=[(1000, 4000, 1500, 5.0, 5.4), (1015, 800, 3000, 2.0, 2.2)],
              puts=[(950, 600, 5000, 1.0, 1.2)])
    f = of.flow_from_chain(c, spot=995.0)
    assert f["call_vol"] == 4800 and f["put_vol"] == 600
    assert f["call_turnover"] == pytest.approx(4800 / 4500, abs=0.01)
    b, why = of.bias(f)
    assert b == "bullish"
    assert f["top"][0]["strike"] == 1000 and f["top"][0]["turnover"] == pytest.approx(2.67, abs=0.01)
    assert of.gate("bullish", f)[0] and not of.gate("bearish", f)[0]


def test_put_block_reads_bearish(of):
    c = chain(calls=[(1000, 300, 5000, 5.0, 5.4)], puts=[(950, 3000, 1000, 1.0, 1.2)])
    f = of.flow_from_chain(c, spot=995.0)
    assert of.bias(f)[0] == "bearish"
    assert not of.gate("bullish", f)[0] and of.gate("bearish", f)[0]


def test_churn_is_neutral_even_when_lopsided(of):
    # 3x more calls than puts, but only a tenth of open interest turned over:
    # an existing book being traded, not new positioning.
    c = chain(calls=[(1000, 900, 20000, 5.0, 5.4)], puts=[(950, 300, 4000, 1.0, 1.2)])
    f = of.flow_from_chain(c, spot=995.0)
    assert of.bias(f)[0] == "neutral"
    assert of.gate("bullish", f)[0] and of.gate("bearish", f)[0]


def test_thin_volume_is_neutral(of):
    c = chain(calls=[(1000, 200, 100, 5.0, 5.4)], puts=[(950, 10, 100, 1.0, 1.2)])
    assert of.bias(of.flow_from_chain(c, 995.0))[0] == "neutral"


def test_empty_and_summed_chains(of):
    assert of.flow_from_chain({}, 1.0) is None
    a = chain(calls=[(1000, 1500, 1000, 5.0, 5.4)], puts=[])
    b = chain(calls=[(1000, 1500, 1000, 5.0, 5.4)], puts=[])
    f = of.flow_from_chain([a, b], 995.0)
    assert f["call_vol"] == 3000 and f["call_oi"] == 2000
    assert "OPTIONS" in of.describe(f) and of.describe(None) == "OPTIONS n/a"
