"""Section 236: an unfilled close is cancelled, never left working on the legs."""

from trading_engine import orphans, tradier_orders

ST = {"root": "QQQ", "long_strike": 743.0, "short_strike": 740.0, "key": "k"}


def _wire(monkeypatch, fill, status_after_cancel):
    calls = {"cancel": [], "fill": 0}
    monkeypatch.setattr(orphans, "_send_close", lambda st, r, p: ("OID", 1))

    def fv(oid):
        calls["fill"] += 1
        return fill(calls["fill"])
    monkeypatch.setattr(orphans, "_fill_value", fv)
    monkeypatch.setattr(tradier_orders, "cancel_order", lambda oid: calls["cancel"].append(oid) or {})
    monkeypatch.setattr(tradier_orders, "order_status", lambda oid: {"status": status_after_cancel})
    return calls


def test_unfilled_stop_is_cancelled(monkeypatch):
    calls = _wire(monkeypatch, lambda n: None, "canceled")
    assert orphans._close(ST, "STOP_LOSS", 1.35) is None
    assert calls["cancel"] == ["OID"]


def test_fill_during_cancel_is_booked(monkeypatch):
    calls = _wire(monkeypatch, lambda n: None if n == 1 else (1.30, 1), "filled")
    assert orphans._close(ST, "STOP_LOSS", 1.35) == (1.30, 1)
    assert calls["cancel"] == ["OID"]


def test_filled_close_is_not_cancelled(monkeypatch):
    calls = _wire(monkeypatch, lambda n: (1.35, 1), "filled")
    assert orphans._close(ST, "STOP_LOSS", 1.35) == (1.35, 1)
    assert calls["cancel"] == []
