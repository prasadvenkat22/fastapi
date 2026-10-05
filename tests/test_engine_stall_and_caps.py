"""Engine stall arm level (TRADING_STALL_ARM_PCT) and the per-day entry caps."""

import importlib
import os
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from trading_engine import nodes






def _dte0():
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
    return importlib.import_module("dte0_trade")


def _order(sym, opening=True, created="2026-10-05T14:00:00Z"):
    return {"legs": [{"symbol": sym}], "opening": opening, "created": created}


def test_entries_today_counts_by_bucket(monkeypatch):
    d = _dte0()
    orders = [
        _order("MU261005C01070000"),                    # stock, same day -> 0DTE
        _order("NVDA261005P00180000"),                  # stock, same day -> 0DTE
        _order("MU261005C01070000", opening=False),     # a close, not an entry
        _order("QQQ261005P00750000"),                   # the engine's bucket
        _order("AMD261009C00200000"),                   # weekly expiry
    ]
    monkeypatch.setattr(d.tradier_orders, "filled_spread_orders", lambda: orders)
    assert d._entries_today("dte0", "261005") == 2
    assert d._entries_today("weekly", "261005") == 1


def test_max_entries_reads_the_bucket_setting(monkeypatch):
    d = _dte0()
    monkeypatch.setenv("TRADING_MAX_ENTRIES_STOCK_0DTE", "2")
    monkeypatch.delenv("TRADING_MAX_ENTRIES_STOCK_W7", raising=False)
    assert d._max_entries("0dte") == 2
    assert d._max_entries("w7") == 0






