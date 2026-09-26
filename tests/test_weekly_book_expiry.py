"""--expiry resolution for the weekly book. Section 198."""
import os, sys
from datetime import date

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts")))


def _resolve():
    import importlib
    import pytest
    pytest.importorskip("yfinance")
    import dte0_trade as m
    return importlib.reload(m)._resolve_expiry


def test_friday_from_each_weekday():
    r = _resolve()
    assert r("friday", "weekly", date(2026, 9, 21)) == "2026-09-25"   # Monday -> this Friday
    assert r("friday", "weekly", date(2026, 9, 23)) == "2026-09-25"   # Wednesday -> this Friday (2 days)
    assert r("friday", "weekly", date(2026, 9, 24)) == "2026-10-02"   # Thursday -> next Friday
    assert r("friday", "weekly", date(2026, 9, 25)) == "2026-10-02"   # Friday -> next Friday
    assert r("friday", "weekly", date(2026, 9, 19)) == "2026-09-25"   # Saturday -> this coming Friday


def test_defaults_and_literals():
    r = _resolve()
    assert r("", "dte0", date(2026, 9, 21)) == "2026-09-21"
    assert r("", "weekly", date(2026, 9, 21)) == "2026-09-25"
    assert r("+7", "weekly", date(2026, 9, 21)) == "2026-10-02"
    assert r("2026-10-09", "weekly", date(2026, 9, 21)) == "2026-10-09"


def test_seven_day_run_picks_next_friday():
    """Section 237: the Thu/Fri run uses --expiry +5."""
    r = _resolve()
    assert r("+5", "weekly", date(2026, 9, 24)) == "2026-10-02"   # Thursday -> 8 days
    assert r("+5", "weekly", date(2026, 9, 25)) == "2026-10-02"   # Friday -> 7 days


def test_weekly_book_has_its_own_rotation_cutoff(monkeypatch):
    import importlib
    import pytest
    pytest.importorskip("yfinance")
    from datetime import datetime
    from zoneinfo import ZoneInfo
    import dte0_trade as m
    m = importlib.reload(m)
    monkeypatch.setattr(m, "ROTATE_CUTOFF", "13:30")
    monkeypatch.setattr(m, "WEEKLY_ROTATE_CUTOFF", "15:30")
    monkeypatch.setattr(m, "_exits_today", lambda syms, now: {})
    at_1350 = datetime(2026, 9, 25, 13, 50, tzinfo=ZoneInfo("America/New_York"))
    monkeypatch.setattr(m, "BOOK", "dte0")
    assert m._rotation_filter(["MU"], at_1350) == []           # 0DTE: past 13:30
    monkeypatch.setattr(m, "BOOK", "weekly")
    assert m._rotation_filter(["MU"], at_1350) == ["MU"]       # weekly: before 15:30


def test_bucket_exposure_counts_by_book(monkeypatch):
    import importlib
    import pytest
    pytest.importorskip("yfinance")
    import dte0_trade as m
    from trading_engine import orphans
    m = importlib.reload(m)
    rows = [
        {"root": "MU", "expiry": "260925", "long_strike": 100, "short_strike": 105, "entry": 2.0, "credit": False, "qty": 3},
        {"root": "MU", "expiry": "261002", "long_strike": 100, "short_strike": 110, "entry": 4.0, "credit": False, "qty": 1},
        {"root": "QQQ", "expiry": "260925", "long_strike": 740, "short_strike": 735, "entry": 3.0, "credit": False, "qty": 5},
    ]
    monkeypatch.setattr(orphans, "open_structures", lambda *a, **k: rows)
    assert m._bucket_exposure("dte0", "260925") == 600.0     # MU same-day only; QQQ is the engine's
    assert m._bucket_exposure("weekly", "260925") == 400.0   # later expiries


def test_weekly_run_type_and_budget(monkeypatch):
    """Section 245: a weekly run is 3-day or 7-day by the expiry it buys."""
    import importlib
    import pytest
    pytest.importorskip("yfinance")
    import dte0_trade as m
    m = importlib.reload(m)
    assert m._weekly_type_for("2026-10-02", date(2026, 9, 28)) == "w3"     # Mon -> Fri, 4 days
    assert m._weekly_type_for("2026-10-02", date(2026, 9, 25)) == "w7"     # Fri -> next Fri, 7
    monkeypatch.setattr(m, "WEEKLY_MAX_BUDGET", 400.0)
    monkeypatch.delenv("TRADING_W7_MAX_BUDGET", raising=False)
    assert m._weekly_budget("w7") == 400.0
    monkeypatch.setenv("TRADING_W7_MAX_BUDGET", "250")
    assert m._weekly_budget("w7") == 250.0


def test_bucket_exposure_splits_weekly_by_type(monkeypatch):
    import importlib
    import pytest
    pytest.importorskip("yfinance")
    import dte0_trade as m
    from trading_engine import orphans
    m = importlib.reload(m)
    rows = [   # both expire 2030-10-04 (future, so still weeklies); bought 7 and 3 days out
        {"root": "MU", "expiry": "301004", "long_strike": 100, "short_strike": 110, "entry": 4.0,
         "credit": False, "qty": 1, "opened": "2030-09-27T14:00:00Z"},
        {"root": "MU", "expiry": "301004", "long_strike": 100, "short_strike": 105, "entry": 2.0,
         "credit": False, "qty": 1, "opened": "2030-10-01T14:00:00Z"},
    ]
    monkeypatch.setattr(orphans, "open_structures", lambda *a, **k: rows)
    assert m._bucket_exposure("weekly", "301001", "w7") == 400.0
    assert m._bucket_exposure("weekly", "301001", "w3") == 200.0
