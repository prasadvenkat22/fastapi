"""Mock broker clients — mimic the shape of Tradier/Alpaca account and position
data so the trading graph's logic can be fully exercised before any real broker
connection is wired up. No network calls, no real orders."""

from dataclasses import dataclass
from typing import Optional


@dataclass
class MockPosition:
    """Mirrors the fields execution_risk_agent needs from a real open options position."""
    symbol: str
    quantity: int
    entry_cost_basis: float   # per-contract premium paid
    current_market_premium: float  # per-contract current mark


class MockBrokerClient:
    """Drop-in stand-in for a real Alpaca/Tradier client. Configure via the
    constructor to exercise every execution_risk_agent branch (profitable,
    at stop-loss, no position yet) without touching a real account."""

    def __init__(
        self,
        position: Optional[MockPosition] = None,
        available_cash: float = 2500.00,
    ):
        self._position = position
        self._available_cash = available_cash

    def get_open_position(self) -> Optional[MockPosition]:
        return self._position

    def get_available_cash(self) -> float:
        return self._available_cash

    def place_buy_call(self, symbol: str, quantity: int, limit_price: float) -> dict:
        """Mock order placement — logs the intent, places nothing."""
        return {"status": "mock_filled", "action": "BUY_CALL", "symbol": symbol, "quantity": quantity, "limit_price": limit_price}

    def place_buy_more(self, symbol: str, quantity: int, limit_price: float) -> dict:
        return {"status": "mock_filled", "action": "BUY_MORE", "symbol": symbol, "quantity": quantity, "limit_price": limit_price}

    def sell_all(self, symbol: str) -> dict:
        return {"status": "mock_filled", "action": "SELL_ALL", "symbol": symbol}


def default_mock_broker() -> MockBrokerClient:
    """A representative starting position matching the original spec's example
    numbers (a small QQQ call currently down ~10%), useful for manual testing."""
    return MockBrokerClient(
        position=MockPosition(
            symbol="QQQ",
            quantity=1,
            entry_cost_basis=4.50,
            current_market_premium=4.05,
        ),
        available_cash=2500.00,
    )
