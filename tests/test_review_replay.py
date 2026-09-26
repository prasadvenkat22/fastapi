"""Run review() itself, minute by minute, with the broker stubbed.

The 2026-09-24 16:08 outage (UnboundLocalError on zero_dte, orphan management
down for four cycles) shipped because every §229-§232 test read the source text
and none executed the loop. review() swallows exceptions by design, so these
tests fail on the "Orphan review failed" log line instead.
"""

import logging
from datetime import datetime, timedelta, timezone

import pytest

from trading_engine import orphans, tradier_orders

T0 = datetime(2026, 9, 24, 15, 0, tzinfo=timezone.utc)   # 11:00 ET
EXP = "260924"
L, S = f"QQQ{EXP}P00740000", f"QQQ{EXP}P00735000"
KEY = "|".join(sorted((L, S)))
ST = {"root": "QQQ", "expiry": EXP, "right": "P", "long": L, "short": S,
      "long_strike": 740.0, "short_strike": 735.0, "qty": 8, "entry": 3.56,
      "credit": False, "opened": "2026-09-24T15:13:32Z", "key": KEY, "inferred": False}


class Clock:
    now = T0


class FakeDT(datetime):
    @classmethod
    def now(cls, tz=None):
        return Clock.now if tz is None else Clock.now.astimezone(tz)


@pytest.fixture
def harness(tmp_path, monkeypatch):
    m = {"value": 3.56, "closes": [], "books": []}
    monkeypatch.setattr(orphans, "datetime", FakeDT)
    monkeypatch.setattr(orphans, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(orphans, "WATCH_NOW_PATH", str(tmp_path / "watch.json"))
    for name, val in {"WATCH_ORPHANS": True, "MANAGE_ORPHANS": True, "MANAGE_UNDERLYING": set(),
                      "ACCOUNT_FLOOR": 0, "ORPHAN_FORCE_CLOSE": "", "ORPHAN_HOLD_UNTIL": "",
                      "STALL_MINUTES": 5.0, "STALL_GIVEBACK_PCT": 10.0, "ORPHAN_STALL_GIVEBACK_BAND": 0.0,
                      "ORPHAN_STALL_GIVEBACK_FRACTION": 0.10, "STALL_MIN_GAIN_PCT": 2.0,
                      "ORPHAN_STALL_ARM_PCT": 30.0, "STALL_ON_MARK": False,
                      "PROFIT_EXIT_AT_MID": True}.items():
        monkeypatch.setattr(orphans, name, val)
    monkeypatch.setattr(orphans, "open_structures", lambda *_a, **_k: [dict(ST)])

    def mark(st):
        v = m["value"]
        return v, (v - 3.56) / 3.56 * 100.0
    monkeypatch.setattr(orphans, "_mark", mark)
    monkeypatch.setattr(orphans, "_session_tape", lambda *_a, **_k: None)
    monkeypatch.setattr(orphans, "_session_vwap", lambda *_a, **_k: None)
    monkeypatch.setattr(orphans, "_atr_for", lambda *_a, **_k: None)
    monkeypatch.setattr(tradier_orders, "working_leg_symbols", lambda *_a, **_k: set())

    def quotes(syms):   # long leg (740) around value + 6.00, short leg (735) around 6.00, any expiry
        out = {}
        for sym in syms:
            if sym.endswith("P00740000"):
                out[sym] = {"bid": m["value"] + 5.90, "ask": m["value"] + 6.30}
            elif sym.endswith("P00735000"):
                out[sym] = {"bid": 5.90, "ask": 6.10}
        return out
    monkeypatch.setattr(tradier_orders, "quotes", quotes)

    def close(kind):
        def _c(st, reason, price):
            m["closes"].append((kind, reason, round(price, 2)))
            return price, st["qty"]
        return _c
    monkeypatch.setattr(orphans, "_close", close("natural"))
    monkeypatch.setattr(orphans, "_close_at_mid", close("mid"))
    monkeypatch.setattr(orphans, "_book", lambda st, filled, pct, reason, qty=None: m["books"].append(reason))
    return m


def _cycle(harness, caplog, minute, value):
    Clock.now = T0 + timedelta(minutes=minute)
    harness["value"] = value
    with caplog.at_level(logging.INFO, logger=orphans.logger.name):
        orphans.review()
    failed = [r for r in caplog.records if "Orphan review failed" in r.getMessage()]
    assert not failed, failed[0].exc_text or failed[0].getMessage()


def test_watch_now_on_the_sale_price_sells_at_the_mid(harness, caplog):
    _cycle(harness, caplog, 0, 3.92)                 # +10.1% on the sale price
    orphans.request_watch_now(KEY, who="op")
    _cycle(harness, caplog, 1, 3.92)                 # applied: watch from +10.1%
    assert "WATCH NOW" in caplog.text
    _cycle(harness, caplog, 2, 4.00)                 # new high +12.4%, clock restarts
    _cycle(harness, caplog, 4, 3.96)                 # +11.2%: slipped, but only 2 min quiet
    assert harness["closes"] == []
    _cycle(harness, caplog, 8, 3.95)                 # +11.0% <= 12.4 - 1.24, 6 min quiet
    assert harness["closes"] and harness["closes"][0][:2] == ("mid", "STALL")
    assert harness["books"] == ["STALL"]


def test_default_settings_cycle_runs_clean(harness, caplog, monkeypatch):
    monkeypatch.setattr(orphans, "PROFIT_EXIT_AT_MID", False)
    for i, v in enumerate((3.56, 3.70, 3.40, 3.60)):
        _cycle(harness, caplog, i, v)


# --- Section 243: 3-day / 7-day spreads ------------------------------------

WEEKLY = dict(ST, expiry="261002", long=f"QQQ261002P00740000", short=f"QQQ261002P00735000",
              key="QQQ261002P00735000|QQQ261002P00740000")


def test_weekly_type_is_fixed_at_purchase():
    from datetime import date
    assert orphans.weekly_type(dict(WEEKLY, opened="2026-09-25T14:00:00Z"), today=date(2026, 9, 29)) == "w7"
    assert orphans.weekly_type(dict(WEEKLY, opened="2026-09-28T14:00:00Z"), today=date(2026, 9, 29)) == "w3"
    assert orphans.weekly_type(dict(ST), today=date(2026, 9, 24)) is None      # same day


def test_type_settings_fall_back_until_set(monkeypatch):
    st = dict(WEEKLY, opened="2026-09-24T14:00:00Z")          # 8 days -> w7
    monkeypatch.delenv("TRADING_W7_STOP_PCT", raising=False)
    assert orphans.type_setting(st, "STOP_PCT", -20.0) == -20.0
    monkeypatch.setenv("TRADING_W7_STOP_PCT", "-12")
    assert orphans.type_setting(st, "STOP_PCT", -20.0) == -12.0
    assert orphans.later_params(st)["stop_pct"] == -12.0
    assert orphans.later_params(st)["type"] == "w7"


def test_weekly_flatten_closes_at_its_time(harness, caplog, monkeypatch):
    monkeypatch.setattr(orphans, "open_structures",
                        lambda *_a, **_k: [dict(WEEKLY, opened="2026-09-24T14:00:00Z")])
    monkeypatch.setenv("TRADING_W7_FLATTEN", "true")
    monkeypatch.setenv("TRADING_W7_FLATTEN_AT", "11:30")
    _cycle(harness, caplog, 0, 3.60)                          # 11:00 ET: holds
    assert harness["closes"] == []
    _cycle(harness, caplog, 31, 3.60)                         # 11:31 ET: flattens
    assert harness["closes"] and harness["closes"][0][1] == "WEEKLY_FLATTEN"
    assert harness["books"] == ["WEEKLY_FLATTEN"]


def test_weekly_flatten_off_by_default(harness, caplog, monkeypatch):
    monkeypatch.setattr(orphans, "open_structures",
                        lambda *_a, **_k: [dict(WEEKLY, opened="2026-09-24T14:00:00Z")])
    monkeypatch.delenv("TRADING_W7_FLATTEN", raising=False)
    _cycle(harness, caplog, 60, 3.60)                         # 12:00 ET
    assert harness["closes"] == []
