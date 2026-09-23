"""A pair closed today (opened in an earlier session) and re-bought smaller
must be managed at the NEW size and cost, not the day-old cache (2026-09-23,
MU 1070/1080: x20 @ 4.91 yesterday, closed x20, re-bought x4 @ 6.05)."""

from trading_engine import orphans as o

L, S = "MU260923C01070000", "MU260923C01080000"


def _run(monkeypatch, orders, held, cache):
    monkeypatch.setattr(o.tradier_orders, "filled_spread_orders", lambda: orders)
    monkeypatch.setattr(o.tradier_orders, "open_positions",
                        lambda: [{"symbol": k, "quantity": v} for k, v in held.items()])
    monkeypatch.setattr(o.tradier_orders, "filled_legs", lambda: {})
    monkeypatch.setattr(o, "_load", lambda: {"peaks": {}, "structures": cache})
    return {s["key"]: s for s in o.open_structures()}


def _order(opening, qty, net, created):
    return {"legs": [{"symbol": L}, {"symbol": S}], "opening": opening, "closing": not opening,
            "qty": qty, "net": net, "credit": not opening, "created": created}


def test_reopened_smaller_uses_new_size_and_cost(monkeypatch):
    cache = {f"{L}|{S}": {"qty": 20, "net": 4.91, "credit": False, "opened": "2026-09-22"}}
    orders = [_order(True, 4, 6.05, "2026-09-23T15:12:20Z"),      # newest first on purpose
              _order(False, 20, -5.00, "2026-09-23T12:50:57Z")]
    got = _run(monkeypatch, orders, {L: 4, S: -4}, cache)
    st = got[f"{L}|{S}"]
    assert st["qty"] == 4 and abs(abs(st["entry"]) - 6.05) < 1e-6


def test_prior_session_position_partly_closed_keeps_the_rest(monkeypatch):
    cache = {f"{L}|{S}": {"qty": 20, "net": 4.91, "credit": False, "opened": "2026-09-22"}}
    orders = [_order(False, 5, -6.00, "2026-09-23T14:00:00Z")]
    got = _run(monkeypatch, orders, {L: 15, S: -15}, cache)
    st = got[f"{L}|{S}"]
    assert st["qty"] == 15 and abs(abs(st["entry"]) - 4.91) < 1e-6


def test_short_leg_that_pays_nothing_is_refused(monkeypatch):
    """TSLA 372.5/417.5 on 2026-09-23: long 13.53, short 0.20 (1.5%)."""
    import importlib, sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
    d = importlib.import_module("dte0_trade")
    monkeypatch.setattr(d, "MIN_SHORT_PAYS_PCT", 10.0)
    monkeypatch.setattr(d, "MIN_EW", 0.2)
    monkeypatch.setattr(d, "MAX_EW", 0.8)
    monkeypatch.setattr(d, "MAX_SHORT_ATR", 9.0)
    monkeypatch.setattr(d, "MAX_EXTRINSIC", 100.0)
    monkeypatch.setattr(d, "MAX_TARGET_ATR", 9.0)
    monkeypatch.setattr(d, "TARGET_PCT", 30.0)
    monkeypatch.setattr(d, "BOOK", "dte0")
    base = dict(w=45.0, spot=384.38, lo=372.5, hi=417.5, direction="bullish", atr=13.3)
    bad = d._passes(dict(base, cost=13.33, long_ask=13.53, short_bid=0.20))
    assert bad and "long call" in bad
    ok = d._passes(dict(base, w=10.0, lo=380, hi=390, cost=6.0, long_ask=9.0, short_bid=3.0))
    assert ok is None or "long call" not in ok


def test_strike_guard_source_arms_only_after_pinned():
    """Guard must not run on a spread that never crossed its short strike."""
    import inspect
    src = inspect.getsource(o.review)
    assert 'rec["strike_armed"] = True' in src
    assert 'breach and bool(rec.get("strike_armed"))' in src


def test_new_peak_updates_the_record_instead_of_replacing_it():
    import inspect
    src = inspect.getsource(o.review)
    assert "rec = dict(rec, peak_iv=iv_now" in src
    assert 'rec = {"peak_iv": iv_now, "peak_at": now.isoformat(),\n                       "peak_entry": entry_abs}\n            elif' not in src


def test_under_stop_line_is_break_even_plus_cushion():
    # MU 1070/1080 x18 @ 5.64 -> 1075.64; MU 1065/1075 x19 @ 6.46 -> 1071.46
    assert abs(o.under_stop_line("C", 1070.0, 5.64) - 1075.64) < 1e-9
    assert abs(o.under_stop_line("C", 1065.0, 6.46, 1.0) - 1072.46) < 1e-9
    assert abs(o.under_stop_line("P", 400.0, 3.0) - 397.0) < 1e-9


def test_under_stop_is_ahead_of_the_intrinsic_hold_off():
    import inspect
    src = inspect.getsource(o.review)
    assert src.index('reason = "UNDER_STOP"') < src.index("zero_dte and ret_pct <= stop_pct and intrinsic_ok")


def test_under_stop_arm_first_and_prearm_stop_are_wired():
    import inspect
    src = inspect.getsource(o.review)
    assert 'rec["under_armed"] = True' in src
    assert "armed and uread is not None" in src
    assert "stop_pct = ORPHAN_PREARM_STOP_PCT" in src


def test_profit_lock_is_wired_ahead_of_the_hold_off():
    import inspect
    src = inspect.getsource(o.review)
    assert 'rec["lock_armed"] = True' in src
    assert src.index('reason = "PROFIT_LOCK"') < src.index("zero_dte and ret_pct <= stop_pct and intrinsic_ok")
