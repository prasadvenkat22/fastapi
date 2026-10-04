"""Engine stall arm level (TRADING_STALL_ARM_PCT) and the per-day entry caps."""

import importlib
import os
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from trading_engine import nodes


def _pos(minutes_since_peak):
    return SimpleNamespace(peak_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_since_peak))


def _stall(monkeypatch, arm=0.0, minutes=5.0, giveback=3.3):
    monkeypatch.setattr(nodes, "STALL_ARM_PCT", arm)
    monkeypatch.setattr(nodes, "STALL_MINUTES", minutes)
    monkeypatch.setattr(nodes, "STALL_GIVEBACK_PCT", giveback)


def test_stall_fires_after_quiet_minutes_and_giveback(monkeypatch):
    _stall(monkeypatch)
    assert nodes._stalled_peak(_pos(6), 28.0, 24.0) is not None


def test_stall_waits_for_its_arm_level(monkeypatch):
    _stall(monkeypatch, arm=30.0)
    assert nodes._stalled_peak(_pos(20), 28.0, 20.0) is None
    assert nodes._stalled_peak(_pos(20), 31.0, 20.0) is not None


def test_stall_holds_inside_the_window_or_the_giveback(monkeypatch):
    _stall(monkeypatch)
    assert nodes._stalled_peak(_pos(2), 28.0, 20.0) is None      # too soon
    assert nodes._stalled_peak(_pos(10), 28.0, 26.0) is None     # within 3.3 pts


def test_stall_off_when_either_knob_is_zero(monkeypatch):
    _stall(monkeypatch, minutes=0)
    assert nodes._stalled_peak(_pos(60), 40.0, 0.0) is None
    _stall(monkeypatch, giveback=0)
    assert nodes._stalled_peak(_pos(60), 40.0, 0.0) is None


def _dte0():
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
    return importlib.import_module("dte0_trade")


def _order(sym, opening=True, created="2026-10-05T14:00:00Z"):
    return {"legs": [{"symbol": sym}], "opening": opening, "created": created}


def test_entries_today_counts_by_bucket(monkeypatch):
    d = _dte0()
    orders = [
        _order("MU261005C01070000"),                    # stock, same day -> 0DTE
        _order("NVDA261005P00180000"),                  # stock, same day -> 0DTE
        _order("MU261005C01070000", opening=False),     # a close, not an entry
        _order("QQQ261005P00750000"),                   # the engine's bucket
        _order("AMD261009C00200000"),                   # weekly expiry
    ]
    monkeypatch.setattr(d.tradier_orders, "filled_spread_orders", lambda: orders)
    assert d._entries_today("dte0", "261005") == 2
    assert d._entries_today("weekly", "261005") == 1


def test_max_entries_reads_the_bucket_setting(monkeypatch):
    d = _dte0()
    monkeypatch.setenv("TRADING_MAX_ENTRIES_STOCK_0DTE", "2")
    monkeypatch.delenv("TRADING_MAX_ENTRIES_STOCK_W7", raising=False)
    assert d._max_entries("0dte") == 2
    assert d._max_entries("w7") == 0


def test_band_only_overrides_window_tier_lists(monkeypatch):
    from trading_engine import playbook as PB
    w = PB.window_by_playbook("MORNING_DRIFT")
    monkeypatch.setattr(PB, "BAND_ONLY", False)
    assert w.allows_tier("CLEAN") and not w.allows_tier("RELAXED")
    monkeypatch.setattr(PB, "BAND_ONLY", True)
    assert not w.allows_tier("CLEAN") and not w.allows_tier("ZONE")
    assert all(w.allows_tier(t) for t in ("STRICT", "RELAXED", "FADE"))


def test_tiers_setting_validates():
    import pytest
    from trading_engine import settings_overrides as so
    assert so.validate("TRADING_MORNING_PUT_TIERS", " strict, relaxed,strict ") == "STRICT,RELAXED"
    assert so.validate("TRADING_MORNING_TIERS", "all") == "ALL"
    with pytest.raises(ValueError):
        so.validate("TRADING_MORNING_TIERS", "BANDS")
