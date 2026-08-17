"""Shared entry point for running one trading cycle and persisting the
result — used by both the manual POST /trading/run-daily-cycle endpoint and
the optional background scheduler (trading_engine/scheduler.py), so the two
never drift out of sync.

Also where realized P&L gets recorded: an OpenPosition row is deleted
whenever a position fully closes, so without writing a TradeHistory row here
first, the result of that trade (profit or loss) would simply disappear."""

from sqlalchemy.orm import Session

from models_pgdb.trading_models import OpenPosition, TradeHistory, TradingLog

from .broker import MockBrokerClient, MockSpreadPosition, estimate_intrinsic_value
from .data_feed import fetch_qqq_bars
from .graph import run_trading_cycle
from .nodes import POSITION_BUDGET, TAKE_PROFIT_PCT, is_past_force_close
from .state import TradingState


def _classify_close_reason(return_pct: float) -> str:
    if is_past_force_close():
        return "FORCE_CLOSE"
    if return_pct >= TAKE_PROFIT_PCT:
        return "TAKE_PROFIT"
    return "STOP_LOSS"


async def execute_and_persist_cycle(db: Session) -> TradingState:
    open_row = db.query(OpenPosition).first()

    pre_close_current_value = None
    pre_close_return_pct = None

    if open_row is not None:
        # No live option-chain feed is wired up — reprice the open spread
        # from intrinsic value against today's live QQQ spot as an honest
        # mocked approximation (ignores time value/greeks).
        spot = float(fetch_qqq_bars()["Close"].iloc[-1])
        current_value = estimate_intrinsic_value(open_row.strategy, open_row.long_strike, open_row.short_strike, spot)
        position = MockSpreadPosition(
            strategy=open_row.strategy,
            underlying=open_row.underlying,
            quantity=open_row.quantity,
            long_strike=open_row.long_strike,
            short_strike=open_row.short_strike,
            entry_net_debit=open_row.entry_net_debit,
            current_net_value=current_value,
        )
        pre_close_current_value = current_value
        pre_close_return_pct = position.return_pct
        spent = open_row.quantity * open_row.entry_net_debit * 100
        broker = MockBrokerClient(position=position, available_cash=max(POSITION_BUDGET - spent, 0))
    else:
        broker = MockBrokerClient(position=None, available_cash=POSITION_BUDGET)

    final_state = await run_trading_cycle(broker=broker)

    # Sync the persisted position with whatever the broker ended up holding —
    # entered, added to, fully closed, or unchanged (HOLD) — so the next
    # cycle picks it up correctly. Updated in place rather than
    # delete+reinsert when it stays open, so opened_at isn't reset to "now"
    # on every HOLD cycle.
    new_position = broker.get_open_position()

    if open_row is not None and new_position is None:
        realized_dollars = (pre_close_current_value - open_row.entry_net_debit) * open_row.quantity * 100
        db.add(TradeHistory(
            strategy=open_row.strategy,
            underlying=open_row.underlying,
            quantity=open_row.quantity,
            long_strike=open_row.long_strike,
            short_strike=open_row.short_strike,
            entry_net_debit=open_row.entry_net_debit,
            exit_net_value=pre_close_current_value,
            realized_pnl_dollars=round(realized_dollars, 2),
            realized_pnl_pct=pre_close_return_pct,
            close_reason=_classify_close_reason(pre_close_return_pct),
            opened_at=open_row.opened_at,
        ))
        db.delete(open_row)
    elif open_row is not None and new_position is not None:
        # Still open — unchanged (HOLD) or updated (BUY_MORE). Update in
        # place, preserving the original opened_at.
        open_row.strategy = new_position.strategy
        open_row.underlying = new_position.underlying
        open_row.quantity = new_position.quantity
        open_row.long_strike = new_position.long_strike
        open_row.short_strike = new_position.short_strike
        open_row.entry_net_debit = new_position.entry_net_debit
    elif open_row is None and new_position is not None:
        db.add(OpenPosition(
            strategy=new_position.strategy,
            underlying=new_position.underlying,
            quantity=new_position.quantity,
            long_strike=new_position.long_strike,
            short_strike=new_position.short_strike,
            entry_net_debit=new_position.entry_net_debit,
        ))

    db.add(TradingLog(
        execution_status=final_state.get("execution_status", ""),
        macd_signal=final_state.get("macd_signal", ""),
        sma_trend=final_state.get("sma_trend", ""),
        bollinger_zone=final_state.get("bollinger_zone", ""),
        market_sentiment=final_state.get("market_sentiment", ""),
        raw_log_payload={k: v for k, v in final_state.items() if k != "messages"},
    ))
    db.commit()

    return final_state
