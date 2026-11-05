"""Section 260: strict 1-minute Bollinger band TOUCH entries."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from trading_engine import nodes, playbook as PB

NY = ZoneInfo("America/New_York")


def test_touch_is_the_only_window_inside_its_hours():
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


# --- Exit order on the engine's own positions (section 261) ---------------

from trading_engine.broker import MockBrokerClient, MockSpreadPosition  # noqa: E402


def _run_exit(monkeypatch, playbook, value, band_spot=None, tp=None, force=False):
    monkeypatch.setattr(nodes, "is_past_force_close", lambda *a, **k: force)
    monkeypatch.setattr(nodes, "_is_within_opening_warmup", lambda: True)   # no re-entry
    monkeypatch.setattr(nodes, "_stop_confirmed", lambda *a: True)
    monkeypatch.setattr(PB, "ENGINE_TAKE_PROFIT_PCT", tp)
    monkeypatch.setattr(PB, "ENGINE_STOP_PCT", -20.0)
    band = None if band_spot is None else {"spot": band_spot, "mid": 750.0, "upper": 751.0,
                                            "lower": 749.0, "touch": None}
    monkeypatch.setattr(nodes, "_band_touch", lambda: band)
    pos = MockSpreadPosition(strategy="BULL_CALL_SPREAD", underlying="QQQ", quantity=1,
                             long_strike=746.0, short_strike=750.0, entry_net_debit=2.00,
                             current_net_value=value, playbook=playbook)
    broker = MockBrokerClient(position=pos)
    return nodes.execution_risk_agent({}, broker)


def test_engine_take_profit_fires_before_the_sma_target(monkeypatch):
    out = _run_exit(monkeypatch, "BAND_TOUCH:TOUCH", 2.60, band_spot=748.0, tp=25.0)
    assert out.get("exit_reason") == "TAKE_PROFIT"


def test_sma_target_books_a_touch_position(monkeypatch):
    out = _run_exit(monkeypatch, "BAND_TOUCH:TOUCH", 2.10, band_spot=750.2)
    assert out.get("exit_reason") == "TAKE_PROFIT"


def test_touch_position_holds_between_stop_and_target(monkeypatch):
    out = _run_exit(monkeypatch, "BAND_TOUCH:TOUCH", 2.10, band_spot=749.5)
    assert not out.get("exit_reason")


def test_retired_window_position_still_gets_the_stop(monkeypatch):
    out = _run_exit(monkeypatch, "MORNING_DRIFT:CLEAN", 1.40)      # -30%
    assert out.get("exit_reason") == "STOP_LOSS"


def test_force_close_beats_everything(monkeypatch):
    out = _run_exit(monkeypatch, "BAND_TOUCH:TOUCH", 2.60, band_spot=748.0, tp=25.0, force=True)
    assert out.get("exit_reason") == "FORCE_CLOSE"


def test_sma_exit_waits_for_the_minimum_profit(monkeypatch):
    monkeypatch.setattr(nodes, "BAND_TOUCH_MIN_PROFIT_PCT", 10.0)
    # +0.5% at the 20-SMA: below the floor, keep holding (the 10-05 12:22 case)
    assert not _run_exit(monkeypatch, "BAND_TOUCH:TOUCH", 2.01, band_spot=750.2).get("exit_reason")
    # +15% at the 20-SMA: books
    assert _run_exit(monkeypatch, "BAND_TOUCH:TOUCH", 2.30, band_spot=750.2).get("exit_reason") == "TAKE_PROFIT"


def test_min_profit_row_is_on_the_qqq_card():
    from trading_engine import settings_overrides as so
    s = so.BY_KEY["TRADING_BAND_TOUCH_MIN_PROFIT_PCT"]
    assert (s.book, s.order, s.default) == ("qqq", 71, "0")


def test_strike_placement_itm_and_atm(monkeypatch):
    w = PB.window_by_playbook("BAND_TOUCH")
    width = w.width
    monkeypatch.setattr(PB, "BAND_TOUCH_ATM", False)
    assert PB.strikes_for(w, 753.0, True) == (753.0 - width, 753.0)        # call: ITM long, ATM short
    assert PB.strikes_for(w, 754.0, False) == (754.0 + width, 754.0)       # put: ITM long, ATM short
    monkeypatch.setattr(PB, "BAND_TOUCH_ATM", True)
    assert PB.strikes_for(w, 753.0, True) == (753.0, 753.0 + width)        # call: ATM long, OTM short
    assert PB.strikes_for(w, 754.0, False) == (754.0, 754.0 - width)       # put: ATM long, OTM short
    from trading_engine import settings_overrides as so
    s = so.BY_KEY["TRADING_BAND_TOUCH_ATM"]
    assert (s.book, s.order, s.default, s.kind) == ("qqq", 45, "false", "bool")


def test_trend_check_blocks_fading_a_trend(monkeypatch):
    monkeypatch.setattr(nodes, "BAND_TOUCH_TREND_CHECK", True)
    monkeypatch.setattr(nodes, "BAND_TOUCH_TREND_MIN", 0.25)
    rising, falling, flat = {"slope": 0.60}, {"slope": -0.60}, {"slope": 0.10}
    assert nodes.trend_refusal(False, rising)            # put into a rising market: refused
    assert nodes.trend_refusal(True, rising) is None     # call with the trend: allowed
    assert nodes.trend_refusal(True, falling)            # call into a falling market: refused
    assert nodes.trend_refusal(False, falling) is None
    assert nodes.trend_refusal(False, flat) is None      # no real trend: both allowed
    monkeypatch.setattr(nodes, "BAND_TOUCH_TREND_CHECK", False)
    assert nodes.trend_refusal(False, rising) is None    # off: never refuses


def test_band_reports_the_sma_slope(monkeypatch):
    closes = [750.0 + 0.05 * i for i in range(40)]       # steady rise
    _feed(monkeypatch, closes, closes[-1])
    b = nodes._band_touch()
    assert b["slope"] is not None and b["slope"] > 0.5



def test_macro_gate_mirrors_for_puts(monkeypatch):
    monkeypatch.setattr(nodes, "BAND_TOUCH_MACRO_GATE", True)
    monkeypatch.setattr(nodes, "_qqq_news_verdict", lambda: None)
    assert nodes.macro_refusal(True, {"tnx_change_bps": 3.0, "oil_change_pct": 0.0})       # yields up: no call
    assert nodes.macro_refusal(True, {"tnx_change_bps": 0.0, "oil_change_pct": 0.8})       # oil up: no call
    assert nodes.macro_refusal(True, {"tnx_change_bps": -1.0, "oil_change_pct": -0.2}) is None
    assert nodes.macro_refusal(False, {"tnx_change_bps": -3.0, "oil_change_pct": 0.0})     # yields down: no put
    assert nodes.macro_refusal(False, {"tnx_change_bps": 3.0, "oil_change_pct": 0.8}) is None
    monkeypatch.setattr(nodes, "_qqq_news_verdict", lambda: "VERY_BEARISH")
    assert nodes.macro_refusal(True, {}) and nodes.macro_refusal(False, {}) is None
    monkeypatch.setattr(nodes, "BAND_TOUCH_MACRO_GATE", False)
    assert nodes.macro_refusal(True, {"tnx_change_bps": 9.0}) is None


def test_macd_check_wants_the_histogram_turning(monkeypatch):
    monkeypatch.setattr(nodes, "BAND_TOUCH_MACD_CHECK", True)
    turning_up = {"macd_hist": -0.05, "macd_hist_prev": -0.08}
    still_down = {"macd_hist": -0.09, "macd_hist_prev": -0.06}
    assert nodes.macd_refusal(True, turning_up) is None
    assert nodes.macd_refusal(True, still_down)
    assert nodes.macd_refusal(False, still_down) is None
    assert nodes.macd_refusal(False, turning_up)


def test_band_reports_macd(monkeypatch):
    closes = [750.0 + (0.1 if i % 3 else -0.1) for i in range(60)]
    _feed(monkeypatch, closes, closes[-1])
    b = nodes._band_touch()
    assert b["macd_hist"] is not None and b["macd_hist_prev"] is not None


def test_fast_day_widens_the_band_only_when_on_and_only_on_a_fast_window(monkeypatch):
    calm = [750.0 + (0.1 if i % 2 else -0.1) for i in range(30)]        # span 0.027%
    fast = [750.0 + i * 0.2 for i in range(30)]                         # span ~0.77%
    monkeypatch.setattr(nodes, "BAND_TOUCH_SD", 2.0)
    monkeypatch.setattr(nodes, "BAND_TOUCH_VOL_PCT", 0.35)
    monkeypatch.setattr(nodes, "BAND_TOUCH_VOL_BARS", 30)
    monkeypatch.setattr(nodes, "BAND_TOUCH_SD_HI", 0.0)                 # off: old band
    assert nodes.effective_band_sd(pd.Series(fast)) == 2.0
    monkeypatch.setattr(nodes, "BAND_TOUCH_SD_HI", 3.0)
    assert nodes.effective_band_sd(pd.Series(fast)) == 3.0
    assert nodes.effective_band_sd(pd.Series(calm)) == 2.0
    assert nodes.effective_band_sd(pd.Series(fast[:10])) == 2.0         # too few bars
    monkeypatch.setattr(nodes, "BAND_TOUCH_SD_HI", 1.5)                 # never narrower
    assert nodes.effective_band_sd(pd.Series(fast)) == 2.0


def test_fast_day_band_moves_the_touch_level(monkeypatch):
    closes = [750.0 + i * 0.2 for i in range(30)]
    monkeypatch.setattr(nodes, "BAND_TOUCH_SD_HI", 0.0)
    _feed(monkeypatch, closes, 757.5)
    narrow = nodes._band_touch()
    monkeypatch.setattr(nodes, "BAND_TOUCH_SD_HI", 3.0)
    wide = nodes._band_touch()
    assert wide["upper"] > narrow["upper"] and wide["lower"] < narrow["lower"]


def test_afternoon_width_switches_at_pm_from(monkeypatch):
    from dataclasses import replace
    from datetime import datetime, time
    from zoneinfo import ZoneInfo
    monkeypatch.setattr(PB, "BAND_TOUCH_ATM", False)
    w = replace(PB.window_by_playbook("BAND_TOUCH"), width=3.0, width_pm=2.0, pm_from=time(13, 0))
    ny = ZoneInfo("America/New_York")
    am, pm = datetime(2026, 11, 5, 12, 59, tzinfo=ny), datetime(2026, 11, 5, 13, 0, tzinfo=ny)
    assert PB.strikes_for(w, 753.0, True, am) == (750.0, 753.0)
    assert PB.strikes_for(w, 753.0, True, pm) == (751.0, 753.0)
    assert PB.strikes_for(w, 754.0, False, pm) == (756.0, 754.0)
    assert PB.strikes_for(w, 753.0, True) == (750.0, 753.0)            # no time = morning width
    one = replace(w, width_pm=None)
    assert PB.strikes_for(one, 753.0, True, pm) == (750.0, 753.0)      # no afternoon width = one width


def test_afternoon_width_settings_are_on_the_qqq_card():
    from trading_engine import settings_overrides as so
    assert so.BY_KEY["TRADING_BAND_TOUCH_WIDTH"].default == "3"
    assert (so.BY_KEY["TRADING_BAND_TOUCH_WIDTH_PM"].book, so.BY_KEY["TRADING_BAND_TOUCH_WIDTH_PM"].default) == ("qqq", "2")
    assert so.BY_KEY["TRADING_BAND_TOUCH_PM_FROM"].default == "13:00"
