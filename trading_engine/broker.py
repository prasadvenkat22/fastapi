"""Mock broker client — mimics the shape of a real Tradier/Alpaca account and
open position so the trading graph's logic can be fully exercised before any
real broker connection is wired up. No network calls, no real orders.

Models defined-risk debit vertical spreads (bull call spread / bear put
spread) rather than a naked long option: buy one ITM leg, sell one ATM leg
against it. Max loss is capped at the net debit paid, unlike a naked long."""

import math
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

BULL_CALL_SPREAD = "BULL_CALL_SPREAD"
BEAR_PUT_SPREAD = "BEAR_PUT_SPREAD"
# Credit verticals: sell the near leg, buy the far one for protection. You are
# paid to open and profit as the spread decays toward zero.
CALL_CREDIT_SPREAD = "CALL_CREDIT_SPREAD"   # bearish/neutral — short call above spot
PUT_CREDIT_SPREAD = "PUT_CREDIT_SPREAD"     # bullish/neutral — short put below spot

CREDIT_STRATEGIES = (CALL_CREDIT_SPREAD, PUT_CREDIT_SPREAD)


def is_credit(strategy: str) -> bool:
    return strategy in CREDIT_STRATEGIES

ITM_OFFSET = 3.0  # long leg is this far ITM relative to the ATM short leg
STRIKE_INCREMENT = 1.0  # QQQ options strike spacing used for ATM rounding

NY = ZoneInfo("America/New_York")
EXPIRY_HOUR, EXPIRY_MINUTE = 16, 0   # QQQ options expire at today's close
SESSION_MINUTES = 6.5 * 60           # 09:30–16:00

# Rough daily move for QQQ as a fraction of spot, used to size the time-value
# term below. ~1% is a normal day; raise it to model a jumpier tape.
DAILY_VOL_PCT = float(os.getenv("TRADING_DAILY_VOL_PCT", "1.0"))


def round_to_strike(price: float, increment: float = STRIKE_INCREMENT) -> float:
    return round(price / increment) * increment


def minutes_to_expiry(now: Optional[datetime] = None) -> float:
    """Minutes left until today's 16:00 ET expiration, clamped at zero."""
    now = now or datetime.now(NY)
    expiry = now.replace(hour=EXPIRY_HOUR, minute=EXPIRY_MINUTE, second=0, microsecond=0)
    return max((expiry - now).total_seconds() / 60.0, 0.0)


def _normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def estimate_spread_value(
    strategy: str,
    long_strike: float,
    short_strike: float,
    spot: float,
    minutes_left: Optional[float] = None,
) -> float:
    """Per-spread value of a debit vertical, including time value.

    Intrinsic alone is not usable here, and the reason is specific to this
    strategy's shape. The long leg sits ITM and the short leg sits ATM, so at
    the moment of entry the spread's intrinsic value already equals its full
    width — price it on intrinsic and a position is worth its maximum the
    instant it opens, showing an immediate paper gain and taking profit on
    the very next cycle no matter what the market did.

    A vertical's value is better read as the width scaled by how likely it is
    to finish in the money. Modelled here as a normal CDF centred on the
    midpoint between the strikes, with the spread of that distribution
    shrinking as expiry approaches:

        value = width * N( (distance past the midpoint) / (sigma * sqrt(t)) )

    That converges to the true expiry payoff — as t goes to zero the CDF
    becomes the hard clamp intrinsic gives — while behaving sensibly
    intraday. It also reproduces the positive theta this structure actually
    has: hold above the short strike and the value drifts up toward the
    width as time runs out, which is the whole reason for buying ITM.
    """
    width = abs(short_strike - long_strike)
    if width == 0:
        return 0.0

    minutes_left = minutes_to_expiry() if minutes_left is None else minutes_left

    # Signed distance from the midpoint, in the direction that profits.
    midpoint = (long_strike + short_strike) / 2.0
    edge = (spot - midpoint) if strategy == BULL_CALL_SPREAD else (midpoint - spot)

    if minutes_left <= 0:
        # At expiry the payoff is the plain clamp.
        return round(max(0.0, min(edge + width / 2.0, width)), 4)

    sigma = spot * (DAILY_VOL_PCT / 100.0) * math.sqrt(minutes_left / SESSION_MINUTES)
    if sigma <= 0:
        return round(max(0.0, min(edge + width / 2.0, width)), 4)

    return round(width * _normal_cdf(edge / sigma), 4)


def estimate_credit_value(
    strategy: str,
    short_strike: float,
    long_strike: float,
    spot: float,
    minutes_left: Optional[float] = None,
) -> float:
    """What it would cost to buy back a credit vertical right now.

    A call credit spread is exactly a short bull call spread on the same two
    strikes, and a put credit spread is a short bear put spread — so the cost
    to close is the debit spread's value, and estimate_spread_value does the
    work. Selling it means profiting as that number falls toward zero.
    """
    if strategy == CALL_CREDIT_SPREAD:
        # short the nearer call, long the further one: equivalent to a bull
        # call spread running from the short strike up to the long strike.
        return estimate_spread_value(BULL_CALL_SPREAD, short_strike, long_strike, spot, minutes_left)
    return estimate_spread_value(BEAR_PUT_SPREAD, short_strike, long_strike, spot, minutes_left)


def estimate_intrinsic_value(strategy: str, long_strike: float, short_strike: float, spot: float) -> float:
    """Expiry payoff only — kept for the force-close path, where the position
    is being valued at expiration and time value is genuinely zero."""
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
    playbook: str = ""        # named strategy that opened it; drives its take-profit target

    @property
    def return_pct(self) -> float:
        # Rounded to avoid float artifacts (e.g. 19.999999999999996 instead of
        # 20.0) missing the take-profit/stop-loss threshold comparisons.
        if self.entry_net_debit == 0:
            return 0.0
        if is_credit(self.strategy):
            # Credit positions are opened for a credit and profit as the
            # spread decays, so the sign flips: entry_net_debit holds the
            # credit received, and the denominator is that credit rather than
            # capital at risk. Expressed this way +100% means the spread went
            # to zero and you kept everything, and -100% is exactly the "buy
            # it back at twice the premium" stop that credit traders use.
            return round(((self.entry_net_debit - self.current_net_value) / self.entry_net_debit) * 100, 4)
        return round(((self.current_net_value - self.entry_net_debit) / self.entry_net_debit) * 100, 4)

    @property
    def capital_at_risk(self) -> float:
        """Dollars that can actually be lost, per spread.

        For a debit position that is the premium paid. For a credit position
        it is width minus credit — larger than the credit collected, which is
        the whole asymmetry of selling premium and the reason sizing cannot
        use the credit as its denominator.
        """
        if is_credit(self.strategy):
            return max(abs(self.short_strike - self.long_strike) - self.entry_net_debit, 0.0)
        return self.entry_net_debit


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

    def estimate_spread_quantity(self, budget: float, net_debit: Optional[float] = None) -> int:
        """How many spread contracts (100 shares/contract) the given dollar
        budget buys at `net_debit`. Real order sizing would query the actual
        option chain; pass the modelled entry price so sizing and fill agree.
        """
        debit = self._mock_net_debit_estimate if net_debit is None else net_debit
        if debit <= 0:
            return 0
        return max(int(budget // (debit * 100)), 0)

    def place_bull_call_spread(self, underlying: str, quantity: int, long_strike: float, short_strike: float,
                               net_debit: Optional[float] = None, playbook: str = "") -> dict:
        """Mock order placement — no network call, no real order. Updates the
        in-memory position so callers (e.g. the router, to persist state
        across cycles) can read back what's now open via get_open_position().

        net_debit should come from estimate_spread_value() so the fill price
        matches what the next cycle reprices the position at; a fill that
        disagrees with the pricing model shows a phantom gain or loss the
        moment the position opens."""
        debit = self._mock_net_debit_estimate if net_debit is None else net_debit
        self._position = MockSpreadPosition(
            strategy=BULL_CALL_SPREAD, underlying=underlying, quantity=quantity,
            long_strike=long_strike, short_strike=short_strike,
            entry_net_debit=debit, current_net_value=debit, playbook=playbook,
        )
        return {
            "status": "mock_filled", "action": BULL_CALL_SPREAD, "underlying": underlying,
            "quantity": quantity, "long_strike": long_strike, "short_strike": short_strike,
        }

    def place_bear_put_spread(self, underlying: str, quantity: int, long_strike: float, short_strike: float,
                              net_debit: Optional[float] = None, playbook: str = "") -> dict:
        debit = self._mock_net_debit_estimate if net_debit is None else net_debit
        self._position = MockSpreadPosition(
            strategy=BEAR_PUT_SPREAD, underlying=underlying, quantity=quantity,
            long_strike=long_strike, short_strike=short_strike,
            entry_net_debit=debit, current_net_value=debit, playbook=playbook,
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
                strategy=pos.strategy, underlying=pos.underlying, quantity=total_qty, playbook=pos.playbook,
                long_strike=pos.long_strike, short_strike=pos.short_strike,
                entry_net_debit=round(weighted_debit, 4), current_net_value=pos.current_net_value,
            )
        return {"status": "mock_filled", "action": "BUY_MORE", "underlying": underlying, "quantity": quantity}

    def estimate_credit_quantity(self, budget: float, credit: float, width: float) -> int:
        """Contracts a budget supports for a credit vertical.

        Sized against capital at risk (width - credit), not the credit
        received. A $3-wide spread sold for $0.40 collects $40 a contract but
        can lose $260, so sizing on the credit would understate the exposure
        by more than 6x.
        """
        risk = (width - credit) * 100
        if risk <= 0:
            return 0
        return max(int(budget // risk), 0)

    def place_credit_spread(self, strategy: str, underlying: str, quantity: int,
                            short_strike: float, long_strike: float,
                            credit: float, playbook: str = "") -> dict:
        """Mock credit vertical. entry_net_debit carries the CREDIT received —
        the field name is kept so persistence and repricing stay uniform, and
        return_pct reads it correctly based on the strategy."""
        self._position = MockSpreadPosition(
            strategy=strategy, underlying=underlying, quantity=quantity,
            long_strike=long_strike, short_strike=short_strike,
            entry_net_debit=credit, current_net_value=credit, playbook=playbook,
        )
        return {
            "status": "mock_filled", "action": strategy, "underlying": underlying,
            "quantity": quantity, "short_strike": short_strike, "long_strike": long_strike,
        }

    def sell_all(self, underlying: str) -> dict:
        self._position = None
        return {"status": "mock_filled", "action": "SELL_ALL", "underlying": underlying}


def default_mock_broker() -> MockBrokerClient:
    """Flat starting state — no open position, TRADING_POSITION_BUDGET cash
    (same env var nodes.POSITION_BUDGET reads, so the two never drift).
    execution_risk_agent falls back to this only when called without an
    explicit broker (e.g. a direct/test call) — the real
    /trading/run-daily-cycle path always builds its own broker via
    trading_engine/service.py, which prices the persisted position instead.
    To exercise the stop-loss/take-profit/buy-more branches directly, pass a
    MockBrokerClient with an explicit MockSpreadPosition instead."""
    budget = float(os.getenv("TRADING_POSITION_BUDGET", "1000"))
    return MockBrokerClient(position=None, available_cash=budget)
