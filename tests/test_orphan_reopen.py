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
