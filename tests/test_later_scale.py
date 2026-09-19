"""The LATER ladder scaled by sessions left. Section 199."""
import importlib
from datetime import date

import pytest


@pytest.fixture
def o(monkeypatch):
    for k, v in {"TRADING_ORPHAN_LATER_STOP_PCT": "-45", "TRADING_ORPHAN_LATER_STOP_MINUTES": "15",
                 "TRADING_ORPHAN_LATER_STALL_ARM": "25", "TRADING_ORPHAN_LATER_STALL_MINUTES": "30",
                 "TRADING_ORPHAN_LATER_STALL_GIVEBACK_ATR": "0.25",
                 "TRADING_ORPHAN_LATER_STALL_GIVEBACK_BAND": "0",
                 "TRADING_ORPHAN_LATER_SCALE": "true", "TRADING_ORPHAN_STATE": "/dev/null"}.items():
        monkeypatch.setenv(k, v)
    import trading_engine.orphans as m
    return importlib.reload(m)


def st(expiry_yymmdd):
    return {"expiry": expiry_yymmdd, "root": "SNDK"}


def test_sessions_count_trading_days_only(o):
    # Saturday 2026-09-19 -> Friday 2026-09-25: Mon..Fri = 5 sessions
    assert o._sessions_to_expiry(st("260925"), date(2026, 9, 19)) == 5
    # Monday -> Friday same week: Tue..Fri = 4
    assert o._sessions_to_expiry(st("260925"), date(2026, 9, 21)) == 4
    # Thursday -> Friday: 1
    assert o._sessions_to_expiry(st("260925"), date(2026, 9, 24)) == 1
    # expiry day itself: 0
    assert o._sessions_to_expiry(st("260925"), date(2026, 9, 25)) == 0


def test_full_week_and_one_session_anchors(o):
    full = o.later_params(st("260925"), date(2026, 9, 19))     # 5 sessions
    assert full["stop_pct"] == -45 and full["stop_minutes"] == 15
    assert full["stall_arm"] == 25 and full["stall_minutes"] == 30 and full["giveback_atr"] == 0.25
    one = o.later_params(st("260925"), date(2026, 9, 24))      # 1 session
    assert one["stop_pct"] == -30 and one["stop_minutes"] == 5
    assert one["stall_arm"] == 5 and one["stall_minutes"] == 10 and one["giveback_atr"] == 0.10


def test_midweek_interpolates(o):
    mid = o.later_params(st("260925"), date(2026, 9, 22))      # Tue -> 3 sessions, f = 0.5
    assert mid["f"] == 0.5
    assert mid["stop_pct"] == pytest.approx(-37.5)
    assert mid["stop_minutes"] == pytest.approx(10)
    assert mid["stall_arm"] == pytest.approx(15)
    assert mid["stall_minutes"] == pytest.approx(20)
    assert mid["giveback_atr"] == pytest.approx(0.175)


def test_beyond_a_week_caps_at_full(o):
    far = o.later_params(st("261016"), date(2026, 9, 19))       # ~19 sessions
    assert far["f"] == 1.0 and far["stop_pct"] == -45


def test_scale_off_restores_flat_numbers(o, monkeypatch):
    monkeypatch.setattr(o, "ORPHAN_LATER_SCALE", False)
    one = o.later_params(st("260925"), date(2026, 9, 24))
    assert one["stop_pct"] == -45 and one["stall_minutes"] == 30
