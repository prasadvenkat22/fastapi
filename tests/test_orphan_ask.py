"""The ASK state machine in orphans.py, exercised against a faked broker.

Section 193. These are the ten scenarios run before the first deploy, kept
so the next change to the ask path has something to break. No network, no
database: tradier_orders is patched at the module boundary orphans calls
through, and data_feed (which imports yfinance) is replaced with a stub so
the test runs where yfinance is not installed.
"""

import importlib
import os
import sys
import types
from datetime import datetime, timedelta, timezone

import pytest

ENV = {
    "TRADING_ORPHAN_ASK": "true",
    "TRADING_ORPHAN_TARGET_RETURN_PCT": "70",
    "TRADING_ORPHAN_ASK_START_WIDTH": "0.88",
    "TRADING_ORPHAN_ASK_STEP": "0.10",
    "TRADING_ORPHAN_ASK_STEP_MINUTES": "3",
    "TRADING_LIVE_ORDERS": "true",
    "TRADING_MAX_ORDER_CONTRACTS": "5",
}

ST = {"root": "MU", "right": "C", "long_strike": 1000.0, "short_strike": 1015.0,
      "qty": 9, "entry": 7.24, "credit": False, "expiry": "260921",
      "long": "MU260921C01000000", "short": "MU260921C01015000",
      "key": "MU|260921|C|1000|1015"}
WIDTH = 15.0


class FakeBroker:
    def __init__(self):
        self.orders = {}
        self.n = 100

    def submit_vertical(self, underlying, expiry, call_put, long_strike, short_strike,
                        quantity, opening, limit_price, is_credit, preview=None):
        self.n += 1
        oid = str(self.n)
        self.orders[oid] = {"status": "open", "price": limit_price, "qty": quantity}
        return {"id": oid, "status": "ok"}

    def order_status(self, oid):
        return dict(self.orders.get(str(oid), {"status": "rejected"}))

    def cancel_order(self, oid):
        if self.orders[str(oid)]["status"] == "open":
            self.orders[str(oid)]["status"] = "canceled"
        return {"status": "ok"}

    @staticmethod
    def open_positions():
        return [{"symbol": ST["long"], "quantity": 9}, {"symbol": ST["short"], "quantity": -9}]


@pytest.fixture
def world(monkeypatch):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("TRADING_ORPHAN_STATE", os.devnull)
    market = {"spot": 1024.0, "vwap": 1025.0}
    monkeypatch.setitem(sys.modules, "trading_engine.data_feed",
                        types.SimpleNamespace(fetch_spot=lambda sym: market["spot"]))
    import trading_engine.orphans as o
    o = importlib.reload(o)          # knobs are read at import
    broker = FakeBroker()
    for name in ("submit_vertical", "order_status", "cancel_order", "open_positions"):
        monkeypatch.setattr(o.tradier_orders, name, getattr(broker, name))
    clock = {"vwap_from": True, "cancel_by": False}
    monkeypatch.setattr(o, "_past_clock",
                        lambda hhmm: clock["vwap_from"] if hhmm == o.ORPHAN_ASK_VWAP_FROM
                        else clock["cancel_by"])
    monkeypatch.setattr(o, "_session_vwap", lambda root: market["vwap"])
    monkeypatch.setattr(o, "time", types.SimpleNamespace(sleep=lambda s: None, time=o.time.time))
    return types.SimpleNamespace(o=o, broker=broker, market=market, clock=clock)


def _aged(rec, minutes=5):
    rec["ask"]["last_step"] = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


def test_places_at_start_width_with_target_floor(world):
    rec = {}
    world.o._ask_manage(ST, rec, 12.00, 7.24, WIDTH, False)
    a = rec["ask"]
    assert a["price"] == pytest.approx(13.20)      # 88% of 15
    assert a["floor"] == pytest.approx(12.31)      # 7.24 * 1.70
    assert a["steps"] == 0


def test_no_ask_when_bid_already_at_start(world):
    rec = {}
    world.o._ask_manage(ST, rec, 13.50, 7.24, WIDTH, False)
    assert rec.get("ask") is None


def test_holds_above_vwap(world):
    rec = {}
    world.o._ask_manage(ST, rec, 12.00, 7.24, WIDTH, False)
    world.market.update(spot=1026.0, vwap=1025.0)
    _aged(rec)
    world.o._ask_manage(ST, rec, 12.5, 7.24, WIDTH, True)
    assert rec["ask"]["price"] == pytest.approx(13.20)
    assert rec["ask"]["steps"] == 0


def test_steps_down_below_vwap_by_cancel_and_replace(world):
    rec = {}
    world.o._ask_manage(ST, rec, 12.00, 7.24, WIDTH, False)
    first = rec["ask"]["id"]
    world.market.update(spot=1023.0, vwap=1025.0)
    _aged(rec)
    world.o._ask_manage(ST, rec, 12.5, 7.24, WIDTH, True)
    assert world.broker.orders[first]["status"] == "canceled"
    assert rec["ask"]["id"] != first
    assert rec["ask"]["price"] == pytest.approx(13.10)
    assert rec["ask"]["steps"] == 1


def test_no_step_inside_the_interval(world):
    rec = {}
    world.o._ask_manage(ST, rec, 12.00, 7.24, WIDTH, False)
    world.market.update(spot=1023.0, vwap=1025.0)
    world.o._ask_manage(ST, rec, 12.5, 7.24, WIDTH, True)   # last_step is now
    assert rec["ask"]["price"] == pytest.approx(13.20)


def test_walks_to_the_floor_and_stops(world):
    rec = {}
    world.o._ask_manage(ST, rec, 12.00, 7.24, WIDTH, False)
    world.market.update(spot=1023.0, vwap=1025.0)
    for _ in range(15):
        _aged(rec)
        world.o._ask_manage(ST, rec, 12.0, 7.24, WIDTH, True)
    assert rec["ask"]["price"] == pytest.approx(12.31)
    assert rec["ask"]["steps"] == 9                      # 13.20 -> 12.31 in 0.10 steps


def test_fill_racing_the_cancel_is_not_replaced(world):
    rec = {}
    world.o._ask_manage(ST, rec, 12.00, 7.24, WIDTH, False)
    oid = rec["ask"]["id"]
    world.broker.orders[oid]["status"] = "filled"
    world.market.update(spot=1023.0, vwap=1025.0)
    _aged(rec)
    world.o._ask_manage(ST, rec, 12.0, 7.24, WIDTH, True)
    assert rec["ask"]["id"] == oid                       # left for the next pass to book
    assert world.o._ask_cancel(rec["ask"], ST) == "filled"


def test_unconfirmed_cancel_sends_nothing(world):
    rec = {}
    world.o._ask_manage(ST, rec, 12.00, 7.24, WIDTH, False)
    oid = rec["ask"]["id"]
    # The broker accepts the cancel but keeps reporting the order open.
    world.broker.cancel_order = lambda o: {"status": "ok"}
    world.o.tradier_orders.cancel_order = world.broker.cancel_order
    world.market.update(spot=1023.0, vwap=1025.0)
    _aged(rec)
    before = len(world.broker.orders)
    world.o._ask_manage(ST, rec, 12.0, 7.24, WIDTH, True)
    assert len(world.broker.orders) == before            # no replacement was sent
    assert rec["ask"]["id"] == oid


def test_withdrawn_at_cancel_by(world):
    rec = {}
    world.o._ask_manage(ST, rec, 12.00, 7.24, WIDTH, False)
    oid = rec["ask"]["id"]
    world.clock["cancel_by"] = True
    world.o._ask_manage(ST, rec, 12.0, 7.24, WIDTH, True)
    assert rec.get("ask") is None
    assert world.broker.orders[oid]["status"] == "canceled"


def test_no_room_between_cost_and_width(world):
    rec = {}
    world.o._ask_manage(dict(ST, entry=10.0), rec, 12.0, 10.0, WIDTH, False)
    assert rec.get("ask") is None                        # 17.00 floor > 15 width


def test_credit_spreads_are_never_asked(world):
    rec = {}
    world.o._ask_manage(dict(ST, credit=True), rec, 12.0, 7.24, WIDTH, False)
    assert rec.get("ask") is None
