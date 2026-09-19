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
