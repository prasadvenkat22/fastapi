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

    def quotes(syms):   # long leg 20c wide around (value + 6.00), short 20c wide around 6.00
        return {L: {"bid": m["value"] + 5.90, "ask": m["value"] + 6.30},
                S: {"bid": 5.90, "ask": 6.10}}
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
