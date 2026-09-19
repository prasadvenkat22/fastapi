"""The flow gate's arithmetic and its four tests, on synthetic bars. Section 194."""

import importlib
import sys

import pytest


@pytest.fixture
def vg(monkeypatch):
    monkeypatch.setenv("TRADING_VWAP_MIN_BARS", "3")
    monkeypatch.setenv("TRADING_VWAP_SLOPE_BARS", "6")
    monkeypatch.setenv("TRADING_VWAP_BARS_ABOVE_MIN", "0.60")
    import trading_engine.vwap_gate as m
    return importlib.reload(m)


def bar(o, c, v=1000.0, wick=0.2):
    hi, lo = max(o, c) + wick, min(o, c) - wick
    return {"high": hi, "low": lo, "close": c, "volume": v, "vwap": (o + c) / 2}


def rising(n=10, start=100.0, step=0.5):
    return [bar(start + i * step, start + (i + 1) * step) for i in range(n)]


def falling(n=10, start=100.0, step=0.5):
    return [bar(start - i * step, start - (i + 1) * step) for i in range(n)]


def test_too_few_bars_is_no_read(vg):
    assert vg.flow_from_bars(rising(2)) is None
    ok, why = vg.gate("bullish", None)
    assert not ok and "no tape read" in why


def test_steady_buying_passes_calls_and_refuses_puts(vg):
    f = vg.flow_from_bars(rising())
    assert f["above_vwap"] and f["slope_pct"] > 0 and f["bars_above"] >= 0.6 and f["ad_ratio"] > 0
    assert vg.gate("bullish", f)[0]
    ok, why = vg.gate("bearish", f)
    assert not ok and "over VWAP" in why


def test_steady_selling_passes_puts_and_refuses_calls(vg):
    f = vg.flow_from_bars(falling())
    assert vg.gate("bearish", f)[0]
    ok, why = vg.gate("bullish", f)
    assert not ok and "under VWAP" in why


def test_a_late_spike_alone_does_not_pass(vg):
    # Nine flat-to-down bars, then one big up bar: spot is above VWAP but the
    # session was not bought -- slope over 30 min is flat, most bars closed
    # under VWAP. The gate is meant to refuse exactly this.
    bars = falling(9, start=100.0, step=0.2) + [bar(98.2, 101.0, v=3000.0)]
    f = vg.flow_from_bars(bars)
    assert f["above_vwap"]
    ok, why = vg.gate("bullish", f)
    assert not ok and "bars above" in why


def test_spot_override_is_used_for_the_side_test(vg):
    f = vg.flow_from_bars(rising(), spot=90.0)
    assert f["spot"] == 90.0 and not f["above_vwap"]


def test_bars_above_is_mirrored_for_puts(vg):
    # 7 of 10 bars below VWAP is a 70% "below" reading: passes the put side
    # of the bars test even though only 30% are above.
    f = vg.flow_from_bars(falling())
    assert f["bars_above"] <= 0.4
    assert vg.gate("bearish", f)[0]


def test_describe_never_raises(vg):
    assert vg.describe(None) == "FLOW n/a"
    assert "VWAP" in vg.describe(vg.flow_from_bars(rising()))
