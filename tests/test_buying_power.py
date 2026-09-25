"""Section 238: no opening order without the money for it."""

import pytest

from trading_engine import service, tradier_orders as t


@pytest.fixture
def live(monkeypatch):
    monkeypatch.setattr(t, "LIVE_ORDERS", True)
    monkeypatch.setattr(t, "REQUIRE_BUYING_POWER", True)
    sent = []
    monkeypatch.setattr(t, "_post_order", lambda payload, preview: sent.append(payload) or {"id": 1, "status": "ok"})
    monkeypatch.setattr(t, "open_positions", lambda: [])
    return sent


def _open(**kw):
    args = dict(underlying="MU", expiry="2026-09-25", call_put="call", long_strike=100.0,
                short_strike=105.0, quantity=2, opening=True, limit_price=1.50, is_credit=False,
                preview=False)
    args.update(kw)
    return t.submit_vertical(**args)


def test_requirement_debit_and_credit():
    assert t.opening_requirement(100, 105, 2, 1.50, False) == pytest.approx(300.0)
    assert t.opening_requirement(100, 105, 2, 1.50, True) == pytest.approx(700.0)


def test_unaffordable_open_is_not_sent(live, monkeypatch):
    monkeypatch.setattr(t, "buying_power", lambda: 131.68)
    res = _open()                                   # needs $300
    assert res["status"] == "refused" and live == []


def test_affordable_open_is_sent(live, monkeypatch):
    monkeypatch.setattr(t, "buying_power", lambda: 1000.0)
    assert _open()["status"] == "ok" and len(live) == 1


def test_unreadable_buying_power_refuses_opens_but_never_closes(live, monkeypatch):
    monkeypatch.setattr(t, "buying_power", lambda: None)
    assert _open()["status"] == "refused"
    assert _open(opening=False)["status"] == "ok"   # exits always go out
    assert len(live) == 1


def test_buying_power_reads_each_account_type(monkeypatch):
    monkeypatch.setattr(t, "account_snapshot", lambda: {"margin": {"option_buying_power": 131.68}})
    assert t.buying_power() == 131.68
    monkeypatch.setattr(t, "account_snapshot", lambda: {"cash": {"cash_available": 50.0}})
    assert t.buying_power() == 50.0
    monkeypatch.setattr(t, "account_snapshot", lambda: {"error": 401})
    assert t.buying_power() is None


def test_engine_treats_refused_open_as_rejected():
    assert service._open_rejected({"status": "refused", "reason": "insufficient_buying_power"})


# --- Section 239: the QQQ engine sizes against buying power ------------------

from trading_engine import nodes


def test_engine_sizes_down_to_what_the_account_can_pay(monkeypatch):
    monkeypatch.setattr(t, "LIVE_ORDERS", True)
    monkeypatch.setattr(t, "buying_power", lambda: 131.68)
    assert nodes.cap_to_buying_power(5, 60.0) == 2        # $131.68 / $60 = 2
    assert nodes.cap_to_buying_power(5, 150.0) == 0       # cannot pay for one: no entry
    assert nodes.cap_to_buying_power(1, 60.0) == 1        # already fits


def test_engine_unreadable_buying_power_means_no_entry(monkeypatch):
    monkeypatch.setattr(t, "LIVE_ORDERS", True)
    monkeypatch.setattr(t, "buying_power", lambda: None)
    assert nodes.cap_to_buying_power(3, 60.0) == 0


def test_engine_paper_sizing_is_unchanged(monkeypatch):
    monkeypatch.setattr(t, "LIVE_ORDERS", False)
    monkeypatch.setattr(t, "buying_power", lambda: 0.0)
    assert nodes.cap_to_buying_power(5, 60.0) == 5


def test_engine_entry_path_calls_the_cap():
    import inspect
    src = inspect.getsource(nodes)
    i = src.index("quantity = cap_to_buying_power(quantity, structural_per_contract)")
    assert i < src.index("if is_credit_window and 0 < quantity and net_debit < MIN_CREDIT:")
