"""Section 260: strict 1-minute Bollinger band TOUCH entries."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from trading_engine import nodes, playbook as PB

NY = ZoneInfo("America/New_York")


def test_mode_off_never_returns_the_touch_window(monkeypatch):
    monkeypatch.setattr(PB, "BAND_TOUCH_MODE", False)
    w = PB.window_for(datetime(2026, 10, 5, 15, 0, tzinfo=NY))
    assert w is None or w.name != "BAND_TOUCH"


def test_mode_on_is_the_only_window_inside_its_hours(monkeypatch):
    monkeypatch.setattr(PB, "BAND_TOUCH_MODE", True)
    at = datetime(2026, 10, 5, 10, 30, tzinfo=NY)
    for bull in (True, False):
        assert PB.window_for_direction(bull, at).name == "BAND_TOUCH"
    assert PB.window_for(datetime(2026, 10, 5, 9, 35, tzinfo=NY)) is None
    w = PB.window_by_playbook("BAND_TOUCH")
    assert w.allows_tier("TOUCH") and not w.allows_tier("CLEAN") and not w.allows_tier("STRICT")


def test_touch_stop_is_minus_20_unless_the_engine_stop_is_set(monkeypatch):
    d = (30.0, -20.0, -13.0)
    monkeypatch.setattr(PB, "ENGINE_STOP_PCT", None)
    assert PB.thresholds_for("BAND_TOUCH:TOUCH", d)[1] == -20.0
    monkeypatch.setattr(PB, "ENGINE_STOP_PCT", -12.0)
    assert PB.thresholds_for("BAND_TOUCH:TOUCH", d)[1] == -12.0


def _bars(closes):
    return pd.DataFrame({"Close": closes})


def _feed(monkeypatch, closes, spot):
    monkeypatch.setattr(nodes, "fetch_qqq_bars", lambda **k: _bars(closes))
    monkeypatch.setattr(nodes, "fetch_qqq_spot", lambda: spot)
    import trading_engine.data_feed as df
    monkeypatch.setattr(df, "_tradier_quote", lambda s: {"last": spot})


def test_lower_and_upper_touch_and_inside(monkeypatch):
    closes = [750.0 + (0.1 if i % 2 else -0.1) for i in range(20)]     # sd ~0.103
    _feed(monkeypatch, closes, 749.70)
    assert nodes._band_touch()["touch"] == "LOWER"
    _feed(monkeypatch, closes, 750.30)
    assert nodes._band_touch()["touch"] == "UPPER"
    _feed(monkeypatch, closes, 750.05)
    assert nodes._band_touch()["touch"] is None


def test_too_few_bars_is_no_signal(monkeypatch):
    _feed(monkeypatch, [750.0] * 5, 740.0)
    assert nodes._band_touch() is None


def test_exits_never_cross_to_the_natural_by_default():
    from trading_engine import tradier_orders as t
    assert t.MID_STOPS is True and t.MID_EXIT_FALLBACK is False and t.MID_MAX_STEPS == 0
