"""Mid-first orders (section 253): the price ladder, the working loop, and the
net-price conventions for each of the four order directions."""

from types import SimpleNamespace

from trading_engine import service, tradier_orders as t


def test_ladder_paying_steps_up_to_the_natural():
    assert t.mid_ladder(3.295, 3.37, 0.02, 3) == [3.30, 3.32, 3.34, 3.36]
    assert t.mid_ladder(3.295, 3.33, 0.02, 5) == [3.30, 3.32, 3.33]


def test_ladder_collecting_steps_down_to_the_natural():
    assert t.mid_ladder(3.505, 3.41, 0.02, 3) == [3.50, 3.48, 3.46, 3.44]


class FakeBroker:
    """Fills an order once its price reaches `fills_at` (paying side)."""

    def __init__(self, fills_at, monkeypatch):
        self.fills_at, self.orders, self.now = fills_at, {}, 0.0
        monkeypatch.setattr(t, "submit_vertical", self.submit)
        monkeypatch.setattr(t, "order_status", self.status)
        monkeypatch.setattr(t, "cancel_order", self.cancel)

    def submit(self, *a):
        px = a[7]
        oid = len(self.orders) + 1
        self.orders[oid] = {"px": px, "state": "open"}
        return {"id": oid, "status": "ok"}

    def status(self, oid):
        o = self.orders[oid]
        if o["state"] == "open" and o["px"] >= self.fills_at:
            o["state"] = "filled"
        return {"status": o["state"], "avg_fill_price": o["px"],
                "exec_quantity": 1 if o["state"] == "filled" else 0}

    def cancel(self, oid):
        if self.orders[oid]["state"] == "open":
            self.orders[oid]["state"] = "canceled"
        return {}

    def sleep(self, s):
        self.now += s

    def clock(self):
        return self.now


def _work(b, opening, fallback):
    return t.work_vertical("QQQ", "2026-10-05", "put", 756, 752, 1, opening,
                           mid=3.295, natural=3.37, is_credit=False,
                           fallback_natural=fallback, sleep=b.sleep, clock=b.clock)


def test_entry_defaults_to_the_mid_only(monkeypatch):
    monkeypatch.setattr(t, "MID_ENTRY_MAX_STEPS", 0)
    b = FakeBroker(3.32, monkeypatch)              # would fill one step up
    r = _work(b, True, False)
    assert r["status"] == "refused"
    assert [o["px"] for o in b.orders.values()] == [3.30]


def test_entry_fills_on_a_middle_rung(monkeypatch):
    monkeypatch.setattr(t, "MID_ENTRY_MAX_STEPS", 3)
    monkeypatch.setattr(t, "MID_ENTRY_WAIT_SECONDS", 6)
    b = FakeBroker(3.32, monkeypatch)
    r = _work(b, True, False)
    assert r["filled"] is True and r["fill_price"] == 3.32 and r["steps"] == 1
    assert [o["state"] for o in b.orders.values()] == ["canceled", "filled"]


def test_entry_that_never_fills_is_refused(monkeypatch):
    b = FakeBroker(9.99, monkeypatch)
    r = _work(b, True, False)
    assert r["status"] == "refused" and r["filled"] is False
    assert all(o["state"] == "canceled" for o in b.orders.values())


def test_close_that_never_fills_ends_at_the_natural_and_stays_working(monkeypatch):
    b = FakeBroker(9.99, monkeypatch)
    r = _work(b, False, True)
    last = b.orders[max(b.orders)]
    assert last["px"] == 3.37 and last["state"] == "open" and r["filled"] is None


def test_time_budget_jumps_a_close_to_the_natural(monkeypatch):
    monkeypatch.setattr(t, "MID_BUDGET_SECONDS", 5.0)
    b = FakeBroker(9.99, monkeypatch)
    _work(b, False, True)
    assert [o["px"] for o in b.orders.values()] == [3.30, 3.37]


def _quotes(monkeypatch, long_q, short_q):
    def q(symbols):
        return {symbols[0]: {"bid": long_q[0], "ask": long_q[1]},
                symbols[1]: {"bid": short_q[0], "ask": short_q[1]}}
    monkeypatch.setattr(service.tradier_orders, "quotes", q)


def test_package_quote_debit(monkeypatch):
    _quotes(monkeypatch, (5.80, 5.90), (2.50, 2.60))      # v = 3.20 / 3.40
    pos = SimpleNamespace(strategy="BEAR_PUT_SPREAD", underlying="QQQ", long_strike=756, short_strike=752)
    mid, nat = service._package_quote(pos, opening=True)
    assert round(mid, 3) == 3.30 and round(nat, 3) == 3.40       # pay up to the ask
    mid, nat = service._package_quote(pos, opening=False)
    assert round(mid, 3) == 3.30 and round(nat, 3) == 3.20       # sell down to the bid


def test_package_quote_credit(monkeypatch):
    _quotes(monkeypatch, (0.40, 0.45), (1.20, 1.30))      # long cheap wing, short dearer
    pos = SimpleNamespace(strategy="PUT_CREDIT_SPREAD", underlying="QQQ", long_strike=740, short_strike=744)
    mid, nat = service._package_quote(pos, opening=True)
    assert round(mid, 3) == 0.825 and round(nat, 3) == 0.75     # collect down to the natural
    mid, nat = service._package_quote(pos, opening=False)
    assert round(mid, 3) == 0.825 and round(nat, 3) == 0.90     # buy back up to the natural


def test_stock_entry_is_refused_when_not_filled_at_mid(monkeypatch):
    import importlib, os, sys
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
    d = importlib.import_module("dte0_trade")
    monkeypatch.setattr(d.tradier_orders, "MID_ORDERS", True)
    monkeypatch.setattr(d.tradier_orders, "quotes", lambda syms: {
        syms[0]: {"bid": 5.0, "ask": 5.2}, syms[1]: {"bid": 2.0, "ask": 2.1}})
    seen = {}

    def work(*a, **k):
        seen.update(k)
        return {"status": "refused", "filled": False, "reason": "not_filled_at_mid"}
    monkeypatch.setattr(d.tradier_orders, "work_vertical", work)
    r = d._send_entry("MU", "2026-10-09", "call", 100, 105, 1, 3.2)
    assert r["status"] == "refused"
    assert round(seen["mid"], 3) == 3.05 and seen["fallback_natural"] is False
