"""Mock broker client — mimics the shape of a real Tradier/Alpaca account and
open position so the trading graph's logic can be fully exercised before any
real broker connection is wired up. No network calls, no real orders.

Models defined-risk debit vertical spreads (bull call spread / bear put
spread) rather than a naked long option: buy one ITM leg, sell one ATM leg
against it. Max loss is capped at the net debit paid, unlike a naked long."""

from dataclasses import dataclass
from typing import Optional

BULL_CALL_SPREAD = "BULL_CALL_SPREAD"
BEAR_PUT_SPREAD = "BEAR_PUT_SPREAD"

ITM_OFFSET = 3.0  # long leg is this far ITM relative to the ATM short leg
STRIKE_INCREMENT = 1.0  # QQQ options strike spacing used for ATM rounding


def round_to_strike(price: float, increment: float = STRIKE_INCREMENT) -> float:
    return round(price / increment) * increment


def estimate_intrinsic_value(strategy: str, long_strike: float, short_strike: float, spot: float) -> float:
    """Approximate a debit vertical spread's current per-spread value from
    intrinsic value only (spot vs. strikes), ignoring time value/greeks —
    there's no live option-chain quote feed wired up, so this is the honest
    mocked stand-in: what the spread would be worth if it expired right now,
    clamped to the spread's width."""
    width = abs(short_strike - long_strike)
    if strategy == BULL_CALL_SPREAD:
        value = spot - long_strike
    else:  # BEAR_PUT_SPREAD
        value = long_strike - spot
    return round(max(0.0, min(value, width)), 4)


@dataclass
class MockSpreadPosition:
    """A 2-leg debit vertical spread: long the ITM leg, short the ATM leg."""
    strategy: str          # BULL_CALL_SPREAD or BEAR_PUT_SPREAD
    underlying: str
    quantity: int           # number of spread contracts
    long_strike: float
    short_strike: float
    entry_net_debit: float    # per-spread cost paid to enter (long premium - short premium)
    current_net_value: float  # per-spread current value (long mark - short mark)

    @property
    def return_pct(self) -> float:
        # Rounded to avoid float artifacts (e.g. 19.999999999999996 instead of
        # 20.0) missing the take-profit/stop-loss threshold comparisons below.
        return round(((self.current_net_value - self.entry_net_debit) / self.entry_net_debit) * 100, 4)


class MockBrokerClient:
    """Drop-in stand-in for a real Alpaca/Tradier client. Configure via the
    constructor to exercise every execution_risk_agent branch (profitable,
    at stop-loss, no position yet) without touching a real account."""

    def __init__(
        self,
        position: Optional[MockSpreadPosition] = None,
        available_cash: float = 2500.00,
        mock_net_debit_estimate: float = 2.00,  # rough mocked cost for a new $3-wide spread
    ):
        self._position = position
        self._available_cash = available_cash
        self._mock_net_debit_estimate = mock_net_debit_estimate

    def get_open_position(self) -> Optional[MockSpreadPosition]:
        return self._position

    def get_available_cash(self) -> float:
        return self._available_cash

    def estimate_spread_quantity(self, budget: float) -> int:
        """How many spread contracts (100 shares/contract) the given dollar
        budget buys at the mocked net-debit estimate. Real order sizing would
        query the actual option chain instead of this mocked estimate."""
        cost_per_contract = self._mock_net_debit_estimate * 100
        return max(int(budget // cost_per_contract), 0)

    def place_bull_call_spread(self, underlying: str, quantity: int, long_strike: float, short_strike: float) -> dict:
        """Mock order placement — no network call, no real order. Updates the
        in-memory position so callers (e.g. the router, to persist state
        across cycles) can read back what's now open via get_open_position()."""
        self._position = MockSpreadPosition(
            strategy=BULL_CALL_SPREAD, underlying=underlying, quantity=quantity,
            long_strike=long_strike, short_strike=short_strike,
            entry_net_debit=self._mock_net_debit_estimate, current_net_value=self._mock_net_debit_estimate,
        )
        return {
            "status": "mock_filled", "action": BULL_CALL_SPREAD, "underlying": underlying,
            "quantity": quantity, "long_strike": long_strike, "short_strike": short_strike,
        }

    def place_bear_put_spread(self, underlying: str, quantity: int, long_strike: float, short_strike: float) -> dict:
        self._position = MockSpreadPosition(
            strategy=BEAR_PUT_SPREAD, underlying=underlying, quantity=quantity,
            long_strike=long_strike, short_strike=short_strike,
            entry_net_debit=self._mock_net_debit_estimate, current_net_value=self._mock_net_debit_estimate,
        )
        return {
            "status": "mock_filled", "action": BEAR_PUT_SPREAD, "underlying": underlying,
            "quantity": quantity, "long_strike": long_strike, "short_strike": short_strike,
        }

    def place_buy_more(self, underlying: str, quantity: int) -> dict:
        pos = self._position
        if pos is not None:
            total_qty = pos.quantity + quantity
            # Weighted-average entry debit across the original lot (at its
            # entry price) and the new lot (bought at today's current value).
            weighted_debit = ((pos.entry_net_debit * pos.quantity) + (pos.current_net_value * quantity)) / total_qty
            self._position = MockSpreadPosition(
                strategy=pos.strategy, underlying=pos.underlying, quantity=total_qty,
                long_strike=pos.long_strike, short_strike=pos.short_strike,
                entry_net_debit=round(weighted_debit, 4), current_net_value=pos.current_net_value,
            )
        return {"status": "mock_filled", "action": "BUY_MORE", "underlying": underlying, "quantity": quantity}

    def sell_all(self, underlying: str) -> dict:
        self._position = None
        return {"status": "mock_filled", "action": "SELL_ALL", "underlying": underlying}


def default_mock_broker() -> MockBrokerClient:
    """Flat starting state — no open position, $1000 QQQ position budget —
    used by /trading/run-daily-cycle since the graph always calls
    execution_risk_agent without an explicit broker. To exercise the
    stop-loss/take-profit/buy-more branches instead, pass a MockBrokerClient
    with an explicit MockSpreadPosition (see the manual test scripts)."""
    return MockBrokerClient(position=None, available_cash=1000.00)
