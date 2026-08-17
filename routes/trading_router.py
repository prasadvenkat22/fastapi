import os
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from config.db_pgrs import SessionLocal
from models_pgdb.trading_models import OpenPosition, TradingLog
from schemas_pgrs.trading_schema import KillSwitchResponse, TradingCycleResponse
from trading_engine.broker import MockBrokerClient, MockSpreadPosition, estimate_intrinsic_value
from trading_engine.data_feed import TradierDataError, fetch_qqq_bars
from trading_engine.graph import run_trading_cycle
from trading_engine.nodes import KILL_SWITCH_PATH, POSITION_BUDGET

router = APIRouter(prefix="/trading", tags=["Trading"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


db_dependency = Annotated[Session, Depends(get_db)]


@router.post("/run-daily-cycle", response_model=TradingCycleResponse)
async def run_daily_cycle(db: db_dependency):
    """Runs the LangGraph decision engine once: technical indicators + market
    sentiment fan in to the execution_risk_agent, which returns a final
    decision against the mocked broker. Writes a TradingLog row either way."""

    if os.path.exists(KILL_SWITCH_PATH):
        raise HTTPException(status_code=400, detail="KILL_SWITCH.txt is present — trading is halted.")

    open_row = db.query(OpenPosition).first()

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
        spent = open_row.quantity * open_row.entry_net_debit * 100
        broker = MockBrokerClient(position=position, available_cash=max(POSITION_BUDGET - spent, 0))
    else:
        broker = MockBrokerClient(position=None, available_cash=POSITION_BUDGET)

    try:
        final_state = await run_trading_cycle(broker=broker)
    except TradierDataError as e:
        raise HTTPException(status_code=424, detail=f"Market-breadth data unavailable: {e}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Trading cycle failed: {e}")

    # Sync the persisted position with whatever the broker ended up holding —
    # entered, added to, or closed out — so the next cycle picks it up correctly.
    new_position = broker.get_open_position()
    if open_row is not None:
        db.delete(open_row)
    if new_position is not None:
        db.add(OpenPosition(
            strategy=new_position.strategy,
            underlying=new_position.underlying,
            quantity=new_position.quantity,
            long_strike=new_position.long_strike,
            short_strike=new_position.short_strike,
            entry_net_debit=new_position.entry_net_debit,
        ))

    log = TradingLog(
        execution_status=final_state.get("execution_status", ""),
        macd_signal=final_state.get("macd_signal", ""),
        sma_trend=final_state.get("sma_trend", ""),
        bollinger_zone=final_state.get("bollinger_zone", ""),
        market_sentiment=final_state.get("market_sentiment", ""),
        raw_log_payload={k: v for k, v in final_state.items() if k != "messages"},
    )
    db.add(log)
    db.commit()

    return TradingCycleResponse(
        execution_status=final_state.get("execution_status", ""),
        macd_signal=final_state.get("macd_signal", ""),
        sma_trend=final_state.get("sma_trend", ""),
        bollinger_zone=final_state.get("bollinger_zone", ""),
        market_sentiment=final_state.get("market_sentiment", ""),
        buy_more_count=final_state.get("buy_more_count", 0),
    )


@router.post("/kill-switch/toggle", response_model=KillSwitchResponse)
async def toggle_kill_switch(action: str = Query(..., pattern="^(ACTIVATE|DEACTIVATE)$")):
    """ACTIVATE creates KILL_SWITCH.txt (blocks /run-daily-cycle and forces
    execution_risk_agent to a HALTED state); DEACTIVATE removes it."""

    if action == "ACTIVATE":
        with open(KILL_SWITCH_PATH, "w") as f:
            f.write("Trading halted via /trading/kill-switch/toggle\n")
    else:
        if os.path.exists(KILL_SWITCH_PATH):
            os.remove(KILL_SWITCH_PATH)

    return KillSwitchResponse(kill_switch_active=os.path.exists(KILL_SWITCH_PATH))
