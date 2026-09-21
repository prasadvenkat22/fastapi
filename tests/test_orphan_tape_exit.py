"""The tape exit's arithmetic (section 211): the running VWAP read from bars
and the direction-keyed 'wrong side, moving against' test. No network."""

import importlib
import os
import sys
import types

import pytest


@pytest.fixture
def o(monkeypatch):
    monkeypatch.setenv("TRADING_ORPHAN_TAPE_EXIT", "true")
    monkeypatch.setenv("TRADING_ORPHAN_TAPE_EXIT_MINUTES", "15")
    monkeypatch.setenv("TRADING_ORPHAN_TAPE_EXIT_SLOPE_BARS", "6")
    monkeypatch.setenv("TRADING_ORPHAN_STATE", os.devnull)
    monkeypatch.setitem(sys.modules, "trading_engine.data_feed",
                        types.SimpleNamespace(fetch_spot=lambda sym: None))
    import trading_engine.orphans as m
    return importlib.reload(m)


def bars(prices, vol=1000.0):
    return [{"vwap": p, "close": p, "volume": vol} for p in prices]


def test_knobs_read_from_env(o):
    assert o.ORPHAN_TAPE_EXIT is True
    assert o.ORPHAN_TAPE_EXIT_MINUTES == 15
    assert o.ORPHAN_TAPE_EXIT_SLOPE_BARS == 6
    assert o.ORPHAN_TAPE_EXIT_LOSERS_ONLY is True


def test_tape_from_bars_running_vwap_and_reference(o):
    # ten falling bars: running VWAP falls, reference is six bars back
    data = bars([110, 109, 108, 107, 106, 105, 104, 103, 102, 101])
    spot, now, ref = o._tape_from_bars(data)
    assert spot == 101.0                      # last close when no spot is passed
    assert now == pytest.approx(105.5)         # mean of the ten equal-volume bars
    assert ref == pytest.approx(108.5)         # running VWAP after four bars
    assert now < ref
    spot2, _, _ = o._tape_from_bars(data, spot=99.5)
    assert spot2 == 99.5                       # a live spot wins over the bar close


def test_tape_from_bars_short_session_uses_first_bar(o):
    data = bars([100, 101, 102])
    _, now, ref = o._tape_from_bars(data)
    assert ref == pytest.approx(100.0) and now == pytest.approx(101.0)


def test_tape_from_bars_no_data_is_none(o):
    assert o._tape_from_bars([]) is None
    assert o._tape_from_bars([{"vwap": None, "volume": 0, "close": 1}]) is None


def test_against_requires_both_side_and_slope(o):
    # call debit: under a FALLING vwap -> against
    assert o._tape_against("C", spot=99.0, vwap_now=100.0, vwap_ref=101.0)
    # under, but the vwap is rising -> not against
    assert not o._tape_against("C", spot=99.0, vwap_now=100.0, vwap_ref=99.5)
    # falling, but spot above it -> not against
    assert not o._tape_against("C", spot=101.0, vwap_now=100.0, vwap_ref=101.0)
    # put debit is the mirror
    assert o._tape_against("P", spot=101.0, vwap_now=100.0, vwap_ref=99.0)
    assert not o._tape_against("P", spot=101.0, vwap_now=100.0, vwap_ref=100.5)
    assert not o._tape_against("P", spot=99.0, vwap_now=100.0, vwap_ref=99.0)
    # anything else is never against
    assert not o._tape_against("X", spot=1.0, vwap_now=2.0, vwap_ref=3.0)


def test_session_tape_caches_and_returns_none_on_failure(o, monkeypatch):
    calls = []

    class _Resp:
        def raise_for_status(self):
            raise RuntimeError("down")

    # The broker client is unconfigured under test; give it a base and headers
    # so the fetch is attempted and then fails at the HTTP layer.
    monkeypatch.setattr(o.tradier_orders, "_base", lambda: "http://tradier.test/v1")
    monkeypatch.setattr(o.tradier_orders, "_headers", lambda: {})
    import httpx as real_httpx
    monkeypatch.setattr(real_httpx, "get", lambda *a, **k: (calls.append(1), _Resp())[1])
    o._TAPE_CACHE.clear()
    assert o._session_tape("MU") is None
    assert o._session_tape("MU") is None       # cached: no second fetch inside 60 s
    assert len(calls) == 1
