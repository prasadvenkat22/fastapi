"""Section 271: stop clock (in a row / total), percent profit lock, engine clock persistence."""
from datetime import datetime, timedelta, timezone

from trading_engine import exit_rules as XR
from trading_engine import nodes, playbook as PB
from trading_engine.broker import MockBrokerClient, MockSpreadPosition

T0 = datetime(2026, 10, 7, 14, 21, tzinfo=timezone.utc)

# The 10-07 QQQ 752/750 put, one reading a minute from 10:21 ET: True = past -10%.
# (-22 -22 -24 -24 -32 -24 -22 -12 -12 -17 | -9.8 | -22 -22 | -4.9 | -9.8 | -17 ...)
TAPE_1007 = [True] * 10 + [False] + [True] * 2 + [False, False] + [True] * 11


def _first_fire(tape, minutes, total):
    rec = {}
    for i, breaching in enumerate(tape):
        held = XR.stop_clock(rec, breaching, T0 + timedelta(minutes=i), total)
        if breaching and held >= minutes - 10 / 60:
            return i
    return None


def test_in_a_row_resets_on_one_recovered_reading():
    # 10 minutes in a row: the real fire came 25 readings in (10:46 ET).
    assert _first_fire(TAPE_1007, 10, total=False) == 25


def test_total_minutes_pause_instead_of_reset():
    # Total: 9 minutes 10:21-10:30, then one more 10:32-10:33.
    assert _first_fire(TAPE_1007, 10, total=True) == 12


def test_total_does_not_count_an_outage():
    rec = {}
    XR.stop_clock(rec, True, T0, True)
    held = XR.stop_clock(rec, True, T0 + timedelta(minutes=30), True)
    assert held == XR.MAX_GAP_MINUTES


def test_switching_modes_clears_the_other_clock():
    rec = {}
    XR.stop_clock(rec, True, T0, True)
    XR.stop_clock(rec, True, T0 + timedelta(minutes=1), True)
    assert XR.stop_clock(rec, True, T0 + timedelta(minutes=2), False) == 0.0
    assert "stop_total" not in rec


def test_profit_lock():
    assert not XR.profit_lock(36.6, 22.0, 30.0, None)          # off
    assert not XR.profit_lock(29.3, 10.0, 30.0, 15.0)          # never armed
    assert not XR.profit_lock(36.6, 22.0, 30.0, 15.0)          # armed, above the floor
    assert XR.profit_lock(36.6, 14.6, 30.0, 15.0)              # armed, back to the floor


# --- engine ----------------------------------------------------------------

def _pos(value, peak=0.0):
    return MockSpreadPosition(strategy="BEAR_PUT_SPREAD", underlying="QQQ", quantity=1,
                              long_strike=752.0, short_strike=750.0, entry_net_debit=0.41,
                              current_net_value=value, playbook="BAND_TOUCH:TOUCH",
                              peak_return_pct=peak, opened_at="2026-10-07T13:59:00+00:00")


def test_engine_stop_clock_survives_a_new_process(monkeypatch, tmp_path):
    monkeypatch.setattr(XR, "ENGINE_CLOCK_PATH", str(tmp_path / "clock.json"))
    monkeypatch.setattr(nodes, "STOP_CONFIRM_MINUTES", 2.0)
    clock = iter([T0, T0 + timedelta(minutes=1), T0 + timedelta(minutes=2)])

    class _DT(datetime):
        @classmethod
        def now(cls, tz=None):
            return next(clock)

    monkeypatch.setattr(nodes, "datetime", _DT)
    p = _pos(0.32)
    assert not nodes._stop_confirmed(p, -22.0, -10.0)
    # Nothing in memory carries over: the file does.
    assert not nodes._stop_confirmed(p, -22.0, -10.0)
    assert nodes._stop_confirmed(p, -22.0, -10.0)


def test_engine_clock_does_not_carry_to_a_new_position(monkeypatch, tmp_path):
    monkeypatch.setattr(XR, "ENGINE_CLOCK_PATH", str(tmp_path / "clock.json"))
    XR.save_engine_clock("QQQ|752/750|old", {"stop_since": T0.isoformat()})
    assert XR.load_engine_clock("QQQ|752/750|new") == {}


def _run(monkeypatch, value, peak, floor, arm=None, tp=30.0):
    monkeypatch.setattr(nodes, "is_past_force_close", lambda *a, **k: False)
    monkeypatch.setattr(nodes, "_is_within_opening_warmup", lambda: True)
    monkeypatch.setattr(nodes, "_stop_confirmed", lambda *a: False)
    monkeypatch.setattr(nodes, "_band_touch", lambda: None)
    monkeypatch.setattr(PB, "ENGINE_TAKE_PROFIT_PCT", tp)
    monkeypatch.setenv("TRADING_ENGINE_PROFIT_LOCK_PCT", "" if floor is None else str(floor))
    monkeypatch.setenv("TRADING_ENGINE_PROFIT_LOCK_ARM_PCT", "" if arm is None else str(arm))
    return nodes.execution_risk_agent({}, MockBrokerClient(position=_pos(value, peak)))


def test_engine_profit_lock_books_after_a_missed_take_profit(monkeypatch):
    # Peaked +36.6% (target 30 missed), now 0.47 = +14.6%, lock at +15.
    assert _run(monkeypatch, 0.47, 36.6, 15.0).get("exit_reason") == "PROFIT_LOCK"


def test_engine_profit_lock_holds_above_the_floor_and_when_off(monkeypatch):
    assert not _run(monkeypatch, 0.50, 36.6, 15.0).get("exit_reason")       # +22%
    assert not _run(monkeypatch, 0.47, 36.6, None).get("exit_reason")       # off


def test_engine_profit_lock_arm_defaults_to_take_profit(monkeypatch):
    assert not _run(monkeypatch, 0.47, 25.0, 15.0).get("exit_reason")       # never reached +30
    assert _run(monkeypatch, 0.47, 25.0, 15.0, arm=20.0).get("exit_reason") == "PROFIT_LOCK"
    assert not _run(monkeypatch, 0.47, 36.6, 15.0, tp=None).get("exit_reason")  # no arm at all


# --- ladder wiring and settings rows ---------------------------------------

def test_ladder_percent_lock_reuses_the_profit_lock_exit():
    import inspect
    from trading_engine import orphans
    src = inspect.getsource(orphans)
    assert src.index("TRADING_ORPHAN_PROFIT_LOCK_PCT") < src.index('reason = "PROFIT_LOCK"')


def test_rows_are_on_the_cards():
    from trading_engine import settings_overrides as so
    want = {
        "TRADING_ENGINE_PROFIT_LOCK_ARM_PCT": ("qqq", 72),
        "TRADING_ENGINE_PROFIT_LOCK_PCT": ("qqq", 73),
        "TRADING_ORPHAN_PROFIT_LOCK_ARM_PCT": ("s0", 72),
        "TRADING_ORPHAN_PROFIT_LOCK_PCT": ("s0", 73),
        "TRADING_STOP_CONFIRM_TOTAL": ("global", 50),
    }
    for key, (book, order) in want.items():
        s = so.BY_KEY[key]
        assert (s.book, s.order) == (book, order), key
    assert so.validate("TRADING_ENGINE_PROFIT_LOCK_PCT", "") == ""
    assert so.validate("TRADING_ORPHAN_PROFIT_LOCK_PCT", "15") == "15"
    assert so.validate("TRADING_STOP_CONFIRM_TOTAL", "on") == "true"
