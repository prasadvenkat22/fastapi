"""The week-anchored VWAP gate, on synthetic bars. Section 210."""

import importlib
from datetime import date

import pytest


@pytest.fixture
def wg(monkeypatch):
    monkeypatch.setenv("TRADING_WEEKLY_VWAP_GATE", "veto")
    monkeypatch.setenv("TRADING_WEEKLY_VWAP_TOL_ATR", "0.25")
    monkeypatch.setenv("TRADING_WEEKLY_VWAP_SLOPE_BARS", "6")
    import trading_engine.weekly_vwap_gate as m
    return importlib.reload(m)


def bar(day, hhmm, o, c, v=1000.0):
    return {"time": f"{day}T{hhmm}:00", "high": max(o, c) + 0.2, "low": min(o, c) - 0.2,
            "close": c, "volume": v, "vwap": (o + c) / 2}


def session(day, start, step, n=12, v=1000.0):
    return [bar(day, f"{9 + (i + 6) // 12:02d}:{((i + 6) % 12) * 5:02d}",
                start + i * step, start + (i + 1) * step, v) for i in range(n)]


def test_week_sessions_runs_monday_through_today(wg, monkeypatch):
    monkeypatch.setattr(wg, "_is_trading_day", lambda d: d.weekday() < 5)
    assert wg.week_sessions(date(2026, 9, 23)) == [date(2026, 9, 21), date(2026, 9, 22),
                                                   date(2026, 9, 23)]
    assert wg.week_sessions(date(2026, 9, 21)) == [date(2026, 9, 21)]


def test_anchor_spans_sessions_and_differs_from_the_session_vwap(wg):
    monday = session("2026-09-21", 100.0, 0.5)          # traded 100 -> 106 on Monday
    tuesday = session("2026-09-22", 110.0, 0.1)         # gapped to 110, drifting
    f = wg.flow_from_bars(monday + tuesday)
    assert f["sessions"] == 2 and f["bars"] == 24
    # Monday's volume pulls the week's anchor well under Tuesday's own VWAP.
    tuesday_only = wg.flow_from_bars(tuesday)
    assert f["vwap_week"] < tuesday_only["vwap_week"]
    assert f["side"] == "ABOVE"


def test_band_makes_at_and_neither_side_passes(wg):
    bars = session("2026-09-21", 100.0, 0.0)             # flat: spot == vwap
    f = wg.flow_from_bars(bars, spot=100.05, tol=0.5)
    assert f["side"] == "AT"
    assert not wg.gate("bullish", f)[0]
    assert not wg.gate("bearish", f)[0]


def test_rising_week_passes_calls_refuses_puts(wg):
    f = wg.flow_from_bars(session("2026-09-21", 100.0, 0.5))
    assert f["side"] == "ABOVE" and f["slope_pct"] > 0
    ok, why = wg.gate("bullish", f)
    assert ok and "paying up" in why
    ok, why = wg.gate("bearish", f)
    assert not ok and "not below" in why and "not falling" in why


def test_falling_week_passes_puts_refuses_calls(wg):
    f = wg.flow_from_bars(session("2026-09-21", 100.0, -0.5))
    assert f["side"] == "BELOW" and f["slope_pct"] < 0
    assert wg.gate("bearish", f)[0]
    assert not wg.gate("bullish", f)[0]


def test_no_read_is_a_refusal(wg):
    assert wg.flow_from_bars([]) is None
    ok, why = wg.gate("bullish", None)
    assert not ok and "no weekly VWAP read" in why


def test_read_stitches_the_week_from_bars_for(wg, monkeypatch):
    import trading_engine.vwap_gate as vg

    calls = []

    def fake_bars(symbol, day):
        calls.append(day)
        return session(day, 100.0, 0.5)

    monkeypatch.setattr(vg, "bars_for", fake_bars)
    monkeypatch.setattr(wg, "_is_trading_day", lambda d: d.weekday() < 5)
    from datetime import datetime

    class _Now(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 23, 9, 50, tzinfo=tz)

    monkeypatch.setattr(wg, "datetime", _Now)
    monkeypatch.setattr("trading_engine.data_feed.fetch_spot", lambda s: None, raising=False)
    f = wg.read("FAKE", atr=2.0)
    assert calls == ["2026-09-21", "2026-09-22", "2026-09-23"]
    assert f["sessions"] == 3 and f["tol"] == 0.5


def test_describe_names_the_read(wg):
    f = wg.flow_from_bars(session("2026-09-21", 100.0, 0.5))
    s = wg.describe(f)
    assert s.startswith("WEEKVWAP spot") and "ABOVE" in s and "1 session(s)" in s
