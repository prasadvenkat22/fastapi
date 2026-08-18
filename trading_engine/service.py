"""Shared entry point for running one trading cycle and persisting the
result — used by both the manual POST /trading/run-daily-cycle endpoint and
the optional background scheduler (trading_engine/scheduler.py), so the two
never drift out of sync.

Also where realized P&L gets recorded: an OpenPosition row is deleted
whenever a position fully closes, so without writing a TradeHistory row here
first, the result of that trade (profit or loss) would simply disappear."""

import logging

from sqlalchemy.orm import Session

from GENAI.vector_stores import VoyageEmbeddings
from models_pgdb.trading_models import OpenPosition, TradeHistory, TradingLog

from .broker import MockBrokerClient, MockSpreadPosition, estimate_spread_value, fill_price
from .data_feed import fetch_qqq_spot
from .equity import current_equity
from .graph import run_trading_cycle
from .nodes import POSITION_BUDGET, TAKE_PROFIT_PCT, is_past_force_close
from .setup_vector_store import store_trade_setup
from .state import TradingState

logger = logging.getLogger(__name__)


def _classify_close_reason(exit_reason: str, return_pct: float) -> str:
    """Prefer the reason the rule engine actually recorded when it closed the
    position (nodes.execution_risk_agent). Re-deriving it from P&L here can't
    tell a RISK_OFF exit from an ordinary stop — both are just "losing" — and
    re-checking the clock could disagree with the agent across a minute
    boundary. The P&L fallback covers a close arriving with no reason set."""
    if exit_reason:
        return exit_reason
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
        # against today's live QQQ spot with the same model that priced the
        # entry (see broker.estimate_spread_value). Repricing on intrinsic
        # alone, as this once did, valued a freshly opened long-ITM/short-ATM
        # spread at its full width immediately: every position showed a paper
        # gain on the next cycle and took profit regardless of the market.
        spot = fetch_qqq_spot()
        # Marked where it could actually be sold — the bid.
        current_value = fill_price(
            estimate_spread_value(open_row.strategy, open_row.long_strike, open_row.short_strike, spot), "sell"
        )
        position = MockSpreadPosition(
            strategy=open_row.strategy,
            underlying=open_row.underlying,
            quantity=open_row.quantity,
            long_strike=open_row.long_strike,
            short_strike=open_row.short_strike,
            entry_net_debit=open_row.entry_net_debit,
            current_net_value=current_value,
            playbook=open_row.playbook or "",
        )
        pre_close_current_value = current_value
        pre_close_return_pct = position.return_pct
        spent = open_row.quantity * open_row.entry_net_debit * 100
        broker = MockBrokerClient(position=position, available_cash=max(POSITION_BUDGET - spent, 0))
    else:
        # Flat: cash available is realized equity, not the static budget, so
        # sizing follows the account rather than a number frozen in the env.
        broker = MockBrokerClient(position=None, available_cash=current_equity(POSITION_BUDGET).equity)

    final_state = await run_trading_cycle(broker=broker)

    # Sync the persisted position with whatever the broker ended up holding —
    # entered, added to, fully closed, or unchanged (HOLD) — so the next
    # cycle picks it up correctly. Updated in place rather than
    # delete+reinsert when it stays open, so opened_at isn't reset to "now"
    # on every HOLD cycle.
    new_position = broker.get_open_position()

    closed_trade = None  # set below if a position fully closed this cycle

    # A cycle can now close one position and open another (same-cycle
    # re-entry after a take-profit), so "was open, is open" no longer implies
    # nothing closed. exit_reason is what actually says a position closed —
    # keying off new_position alone would take the update-in-place branch and
    # silently lose the closed trade's realized P&L.
    closed_this_cycle = bool(final_state.get("exit_reason"))

    if open_row is not None and closed_this_cycle:
        realized_dollars = (pre_close_current_value - open_row.entry_net_debit) * open_row.quantity * 100
        close_reason = _classify_close_reason(final_state.get("exit_reason", ""), pre_close_return_pct)
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
            close_reason=close_reason,
            playbook=open_row.playbook,
            opened_at=open_row.opened_at,
            entry_macd_signal=open_row.entry_macd_signal,
            entry_sma_trend=open_row.entry_sma_trend,
            entry_bollinger_zone=open_row.entry_bollinger_zone,
            entry_rsi_zone=open_row.entry_rsi_zone,
        ))
        closed_trade = {
            "strategy": open_row.strategy,
            "macd_signal": open_row.entry_macd_signal,
            "sma_trend": open_row.entry_sma_trend,
            "bollinger_zone": open_row.entry_bollinger_zone,
            "rsi_zone": open_row.entry_rsi_zone,
            "realized_pnl_pct": pre_close_return_pct,
            "close_reason": close_reason,
        }
        db.delete(open_row)
        db.flush()      # release the row before any replacement is inserted
        open_row = None  # anything opened below is a fresh position

    if new_position is not None:
        if open_row is None:
            db.add(OpenPosition(
                strategy=new_position.strategy,
                underlying=new_position.underlying,
                quantity=new_position.quantity,
                long_strike=new_position.long_strike,
                short_strike=new_position.short_strike,
                entry_net_debit=new_position.entry_net_debit,
                playbook=final_state.get("playbook"),
                entry_macd_signal=final_state.get("macd_signal"),
                entry_sma_trend=final_state.get("sma_trend"),
                entry_bollinger_zone=final_state.get("bollinger_zone"),
                entry_rsi_zone=final_state.get("rsi_zone"),
            ))
        else:
            # Still the same position — unchanged (HOLD) or added to
            # (BUY_MORE). Updated in place so opened_at and the entry signals
            # aren't reset on every cycle.
            open_row.strategy = new_position.strategy
            open_row.underlying = new_position.underlying
            open_row.quantity = new_position.quantity
            open_row.long_strike = new_position.long_strike
            open_row.short_strike = new_position.short_strike
            open_row.entry_net_debit = new_position.entry_net_debit

    db.add(TradingLog(
        execution_status=final_state.get("execution_status", ""),
        macd_signal=final_state.get("macd_signal", ""),
        sma_trend=final_state.get("sma_trend", ""),
        bollinger_zone=final_state.get("bollinger_zone", ""),
        rsi_zone=final_state.get("rsi_zone", ""),
        market_sentiment=final_state.get("market_sentiment", ""),
        raw_log_payload={k: v for k, v in final_state.items() if k != "messages"},
    ))
    db.commit()

    if closed_trade is not None:
        try:
            await store_trade_setup(
                strategy=closed_trade["strategy"],
                macd_signal=closed_trade["macd_signal"],
                sma_trend=closed_trade["sma_trend"],
                bollinger_zone=closed_trade["bollinger_zone"],
                rsi_zone=closed_trade["rsi_zone"],
                realized_pnl_pct=closed_trade["realized_pnl_pct"],
                close_reason=closed_trade["close_reason"],
                embeddings=VoyageEmbeddings(),
            )
        except Exception:
            # Best-effort — a Voyage hiccup here shouldn't undo an already
            # committed, correctly-closed trade.
            logger.exception("Failed to store trade setup vector for closed trade")

    return final_state
