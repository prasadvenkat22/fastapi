"""Section 241: week-range guard and midline triggers."""

import os

from trading_engine import structure_gates as g

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _bars15(days, lo=100.0, hi=110.0, last=None):
    out = []
    for i, d in enumerate(days):
        for k in range(26):                                   # 09:30 .. 15:45
            hh, mm = divmod(570 + 15 * k, 60)
            px = lo + (hi - lo) * ((i * 26 + k) % 20) / 19
            out.append({"time": f"{d}T{hh:02d}:{mm:02d}:00", "high": px + 0.1, "low": px - 0.1, "close": px})
    if last is not None:
        out[-1] = dict(out[-1], close=last, high=max(last, out[-1]["high"]), low=min(last, out[-1]["low"]))
    return out


DAYS = ["2026-09-18", "2026-09-21", "2026-09-22", "2026-09-23", "2026-09-24", "2026-09-25"]


def test_context_uses_the_last_five_sessions_and_computes_position():
    ctx = g.context_from_bars(_bars15(DAYS, last=109.5), [])
    assert ctx["sessions"] == 5
    assert ctx["week_low"] == 99.9 and ctx["week_high"] == 110.1
    assert 0.9 < ctx["weekpos"] <= 1.0
    assert ctx["sma1h"] is not None and ctx["below_1h"] in (True, False)


def test_weekrange_refuses_calls_at_the_high_and_puts_at_the_low(monkeypatch):
    monkeypatch.delenv("TRADING_WEEKRANGE_CALL_MAX", raising=False)
    monkeypatch.delenv("TRADING_WEEKRANGE_PUT_MIN", raising=False)
    high = {"weekpos": 0.95, "week_low": 100, "week_high": 110}
    low = {"weekpos": 0.05, "week_low": 100, "week_high": 110}
    mid = {"weekpos": 0.5, "week_low": 100, "week_high": 110}
    assert g.weekrange_refusal(True, high) and g.weekrange_refusal(True, mid) is None
    assert g.weekrange_refusal(False, low) and g.weekrange_refusal(False, mid) is None
    assert g.weekrange_refusal(False, high) is None          # a put at the high is fine
    assert g.weekrange_refusal(True, low) is None            # a call at the low is fine
    monkeypatch.setenv("TRADING_WEEKRANGE_CALL_MAX", "0.97")
    assert g.weekrange_refusal(True, high) is None


def test_gates_fail_closed_without_bars():
    assert g.weekrange_refusal(True, None)
    assert g.pullback_refusal(True, None, "hourly")
    assert g.pullback_refusal(False, {"below_5m": None}, "5min")


def test_pullback_mirrors_for_puts():
    assert g.pullback_refusal(True, {"below_1h": True}, "hourly") is None
    assert g.pullback_refusal(True, {"below_1h": False}, "hourly")
    assert g.pullback_refusal(False, {"below_1h": False}, "hourly") is None
    assert g.pullback_refusal(False, {"below_1h": True}, "hourly")
    assert g.pullback_refusal(True, {"below_5m": True}, "5min") is None


def test_switches_default_off(monkeypatch):
    for k in ("TRADING_WEEKRANGE_GUARD", "TRADING_PULLBACK_GATE_WEEKLY", "TRADING_PULLBACK_GATE_0DTE",
              "TRADING_MACRO_BAD_PUTS_ONLY"):
        monkeypatch.delenv(k, raising=False)
    assert not g.weekrange_on() and not g.pullback_on("hourly") and not g.pullback_on("5min")
    assert not g.macro_bad_puts_only()


def test_wired_into_the_engine_and_the_rotation():
    nodes = open(os.path.join(REPO, "trading_engine", "nodes.py"), encoding="utf-8").read()
    assert 'structure_gates.weekrange_refusal(bullish, structure_gates.week_context("QQQ"))' in nodes
    rot = open(os.path.join(REPO, "scripts", "dte0_trade.py"), encoding="utf-8").read()
    for needle in ('rejects["week-range guard"]', 'rejects["pullback trigger not met"]',
                   'rejects["bullish with engine macro BAD"]'):
        assert needle in rot
