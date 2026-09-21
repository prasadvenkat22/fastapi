"""The board's Week VWAP column (section 212): the label and the conflict
flag, from a weekly_vwap_gate read. No network."""

import importlib


def _gate():
    import trading_engine.weekly_vwap_gate as g
    return importlib.reload(g)


def flow(side, slope):
    return {"spot": 100.0, "vwap_week": 99.0, "side": side, "tol": 0.5,
            "slope_pct": slope, "bars_above": 0.7, "bars": 40, "sessions": 2}


def test_trend_label():
    g = _gate()
    assert g.trend_label(flow("ABOVE", 0.05)) == "LONG"
    assert g.trend_label(flow("BELOW", -0.05)) == "SHORT"
    assert g.trend_label(flow("ABOVE", -0.05)) == "MIXED"     # side and slope disagree
    assert g.trend_label(flow("AT", 0.05)) == "MIXED"         # inside the band
    assert g.trend_label(None) is None


def test_week_vwap_fields_and_conflicts():
    from routes.trading_router import _week_vwap_fields

    f = _week_vwap_fields(flow("BELOW", -0.1), "bullish")
    assert f["week_vwap_trend"] == "SHORT"
    assert f["week_vwap_conflict"].startswith("bullish structure")
    assert f["week_vwap"] == 99.0 and f["week_vwap_side"] == "BELOW" and f["week_vwap_sessions"] == 2

    f = _week_vwap_fields(flow("ABOVE", 0.1), "bearish")
    assert f["week_vwap_trend"] == "LONG" and f["week_vwap_conflict"].startswith("bearish structure")

    # aligned rows and MIXED weeks carry no conflict
    assert _week_vwap_fields(flow("ABOVE", 0.1), "bullish")["week_vwap_conflict"] is None
    assert _week_vwap_fields(flow("AT", 0.1), "bullish")["week_vwap_conflict"] is None
    # the meta rows pass no direction and never conflict
    assert _week_vwap_fields(flow("BELOW", -0.1), None)["week_vwap_conflict"] is None


def test_no_read_is_all_nulls():
    from routes.trading_router import _week_vwap_fields

    f = _week_vwap_fields(None, "bullish")
    assert all(v is None for v in f.values())
