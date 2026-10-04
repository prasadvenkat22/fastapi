"""The engine's own legs must never be adopted as an orphan (2026-10-02).

Both QQQ engine trades that day (756/752 and 752/749 put debits) were skipped
by the order-history pass, left unconsumed, and then re-paired by the
inferred-leg pass -- so the 0DTE ladder managed the engine's position too, and
closed the second one on its own stop.
"""

from trading_engine import orphans as o

L, S = "QQQ261002P00752000", "QQQ261002P00749000"


def _run(monkeypatch, engine_symbols):
    orders = [{"legs": [{"symbol": L}, {"symbol": S}], "opening": True, "closing": False,
               "qty": 1, "net": 1.88, "credit": False, "created": "2026-10-02T15:55:06Z"}]
    monkeypatch.setattr(o.tradier_orders, "filled_spread_orders", lambda: orders)
    monkeypatch.setattr(o.tradier_orders, "open_positions",
                        lambda: [{"symbol": L, "quantity": 1}, {"symbol": S, "quantity": -1}])
    monkeypatch.setattr(o.tradier_orders, "filled_legs",
                        lambda: {L: {"price": 5.86}, S: {"price": 3.98}})
    monkeypatch.setattr(o, "_load", lambda: {"peaks": {}, "structures": {}})
    return o.open_structures(engine_symbols)


def test_engine_legs_are_not_reinferred(monkeypatch):
    assert _run(monkeypatch, {L, S}) == []


def test_same_legs_without_engine_are_still_adopted(monkeypatch):
    got = _run(monkeypatch, set())
    assert [s["key"] for s in got] == [f"{S}|{L}"]
