"""Section 230: the roll-aware basis must not sum unrelated round trips."""

import pytest

from trading_engine import orphans


@pytest.fixture(autouse=True)
def no_cache(monkeypatch):
    monkeypatch.setattr(orphans, "_load", lambda: {})


def _o(oid, legs, opening=False, closing=False, qty=1):
    net = sum(p if side.startswith("buy") else -p for _, side, p in legs)
    return {"id": oid, "legs": [{"symbol": s, "side": side, "qty": qty, "price": p} for s, side, p in legs],
            "net": round(net, 4), "credit": net < 0, "qty": qty, "opening": opening, "closing": closing,
            "created": str(oid)}


P740, P738, P737, P735 = (f"QQQ260924P00{k}000" for k in (740, 738, 737, 735))


def test_plain_opens_and_closes_on_shared_strikes_are_not_a_roll():
    """The 09-24 shape: repeated same-strike round trips, no roll order anywhere.
    The old walk chained them all into a 740/738 x1 and returned a basis many
    times the 2-point width."""
    orders = [_o(1, [(P738, "buy_to_open", 2.9), (P735, "sell_to_open", 1.4)], opening=True, qty=10),
              _o(2, [(P738, "sell_to_close", 2.1), (P735, "buy_to_close", 0.9)], closing=True, qty=10),
              _o(3, [(P737, "buy_to_open", 1.7), (P735, "sell_to_open", 0.9)], opening=True, qty=15),
              _o(4, [(P737, "sell_to_close", 1.2), (P735, "buy_to_close", 0.6)], closing=True, qty=15),
              _o(5, [(P740, "buy_to_open", 3.1), (P738, "sell_to_open", 1.8)], opening=True, qty=4),
              _o(6, [(P740, "buy_to_open", 3.2), (P737, "sell_to_open", 1.4)], opening=True, qty=10),
              _o(7, [(P740, "sell_to_close", 3.8), (P737, "buy_to_close", 1.7)], closing=True, qty=10),
              _o(8, [(P740, "buy_to_open", 5.1), (P735, "sell_to_open", 1.5)], opening=True, qty=5)]
    for lsym, ssym, qty in ((P740, P738, 1), (P738, P735, 9), (P740, P735, 5)):
        assert orphans._rolled_net(orders, lsym, ssym, qty) is None


L, S1, S2 = "SNDK260918C01605000", "SNDK260918C01630000", "SNDK260918C01680000"


def test_a_real_roll_is_still_priced():
    """2026-09-18: 1605/1630 at 10.60, short rolled to 1680 for 36.30 -> 46.90."""
    orders = [_o(1, [(L, "buy_to_open", 30.0), (S1, "sell_to_open", 19.4)], opening=True),
              _o(2, [(S1, "buy_to_close", 57.0), (S2, "sell_to_open", 20.7)])]
    assert orphans._rolled_net(orders, L, S2, 1) == pytest.approx(46.90)


def test_a_finished_round_trip_on_a_shared_strike_is_ignored():
    other = "SNDK260918C01650000"
    orders = [_o(1, [(L, "buy_to_open", 30.0), (S1, "sell_to_open", 19.4)], opening=True),
              _o(2, [(S1, "buy_to_close", 57.0), (S2, "sell_to_open", 20.7)]),
              _o(3, [(L, "buy_to_open", 31.0), (other, "sell_to_open", 25.0)], opening=True, qty=5),
              _o(4, [(L, "sell_to_close", 33.0), (other, "buy_to_close", 26.0)], closing=True, qty=5)]
    assert orphans._rolled_net(orders, L, S2, 1) == pytest.approx(46.90)


def test_a_basis_above_the_width_is_refused():
    a, b, c = "QQQ260924P00740000", "QQQ260924P00738000", "QQQ260924P00739000"
    orders = [_o(1, [(a, "buy_to_open", 3.0), (c, "sell_to_open", 1.0)], opening=True),
              _o(2, [(c, "buy_to_close", 4.0), (b, "sell_to_open", 1.5)])]   # 2.00 + 2.50 = 4.50 on a 2-wide
    assert orphans._rolled_net(orders, a, b, 1) is None
