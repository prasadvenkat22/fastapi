import os
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from config.db_pgrs import SessionLocal
from models_pgdb.trading_models import OpenPosition, TradeHistory, TradingLog
from schemas_pgrs.trading_schema import (
    KillSwitchResponse,
    OpenPositionResponse,
    SchedulerStatusResponse,
    TradeHistoryResponse,
    TradingCycleResponse,
    TradingStatusResponse,
)
from trading_engine import scheduler
from trading_engine.broker import estimate_intrinsic_value
from trading_engine.data_feed import TradierDataError, fetch_qqq_bars
from trading_engine.nodes import KILL_SWITCH_PATH
from trading_engine.service import execute_and_persist_cycle

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
    decision against the mocked broker. Persists any open position and
    writes a TradingLog row either way."""

    if os.path.exists(KILL_SWITCH_PATH):
        raise HTTPException(status_code=400, detail="KILL_SWITCH.txt is present — trading is halted.")

    try:
        final_state = await execute_and_persist_cycle(db)
    except TradierDataError as e:
        raise HTTPException(status_code=424, detail=f"Market-breadth data unavailable: {e}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Trading cycle failed: {e}")

    return TradingCycleResponse(
        execution_status=final_state.get("execution_status", ""),
        macd_signal=final_state.get("macd_signal", ""),
        sma_trend=final_state.get("sma_trend", ""),
        bollinger_zone=final_state.get("bollinger_zone", ""),
        rsi_zone=final_state.get("rsi_zone", ""),
        market_sentiment=final_state.get("market_sentiment", ""),
        buy_more_count=final_state.get("buy_more_count", 0),
    )


def _build_open_position_response(db: Session) -> OpenPositionResponse:
    row = db.query(OpenPosition).first()
    if row is None:
        return OpenPositionResponse(open=False)

    spot = float(fetch_qqq_bars()["Close"].iloc[-1])
    current_value = estimate_intrinsic_value(row.strategy, row.long_strike, row.short_strike, spot)
    unrealized_pct = round(((current_value - row.entry_net_debit) / row.entry_net_debit) * 100, 2)
    unrealized_dollars = round((current_value - row.entry_net_debit) * row.quantity * 100, 2)

    return OpenPositionResponse(
        open=True,
        strategy=row.strategy,
        underlying=row.underlying,
        quantity=row.quantity,
        long_strike=row.long_strike,
        short_strike=row.short_strike,
        entry_net_debit=row.entry_net_debit,
        current_spot=spot,
        estimated_current_value=current_value,
        unrealized_pnl_pct=unrealized_pct,
        unrealized_pnl_dollars=unrealized_dollars,
        opened_at=row.opened_at,
    )


@router.get("/position", response_model=OpenPositionResponse)
async def get_open_position(db: db_dependency):
    """The currently open spread, if any, repriced live from intrinsic value
    against today's QQQ spot — the unrealized P&L view. No live option-chain
    feed is wired up, so this is a mocked approximation of real premium."""

    return _build_open_position_response(db)


@router.get("/history", response_model=TradeHistoryResponse)
async def get_trade_history(db: db_dependency):
    """Realized P&L from every closed trade, oldest to newest, plus the
    running total — this is where a closed position's profit or loss
    actually lives once its OpenPosition row is gone."""

    rows = db.query(TradeHistory).order_by(TradeHistory.closed_at.asc()).all()
    total = round(sum(r.realized_pnl_dollars for r in rows), 2)
    return TradeHistoryResponse(total_realized_pnl_dollars=total, trade_count=len(rows), trades=rows)


@router.post("/scheduler/start", response_model=SchedulerStatusResponse)
async def start_scheduler(interval_minutes: int = Query(5, ge=1, le=60)):
    """Starts the background loop that re-runs the trading cycle on an
    interval during market hours (9:30AM-4:00PM EST, weekdays) — never
    starts on its own, only via this endpoint. Calling it again while
    already running just reports the current status."""

    scheduler.start(interval_seconds=interval_minutes * 60)
    return SchedulerStatusResponse(
        scheduler_running=scheduler.is_running(),
        interval_seconds=scheduler.get_interval_seconds(),
    )


@router.post("/scheduler/stop", response_model=SchedulerStatusResponse)
async def stop_scheduler():
    scheduler.stop()
    return SchedulerStatusResponse(
        scheduler_running=scheduler.is_running(),
        interval_seconds=scheduler.get_interval_seconds(),
    )


@router.get("/scheduler/status", response_model=SchedulerStatusResponse)
async def scheduler_status():
    return SchedulerStatusResponse(
        scheduler_running=scheduler.is_running(),
        interval_seconds=scheduler.get_interval_seconds(),
    )


@router.get("/status", response_model=TradingStatusResponse)
async def get_trading_status(db: db_dependency):
    """One-call dashboard: kill switch, scheduler, the currently open
    position (with live unrealized P&L), running realized P&L, and the
    most recent cycle's result — everything at a glance."""

    history_rows = db.query(TradeHistory).all()
    total_pnl = round(sum(r.realized_pnl_dollars for r in history_rows), 2)
    last_log = db.query(TradingLog).order_by(TradingLog.timestamp.desc()).first()

    return TradingStatusResponse(
        kill_switch_active=os.path.exists(KILL_SWITCH_PATH),
        scheduler_running=scheduler.is_running(),
        scheduler_interval_seconds=scheduler.get_interval_seconds(),
        position=_build_open_position_response(db),
        total_realized_pnl_dollars=total_pnl,
        closed_trade_count=len(history_rows),
        last_execution_status=last_log.execution_status if last_log else None,
        last_cycle_at=last_log.timestamp if last_log else None,
    )


@router.get("/kill-switch/status", response_model=KillSwitchResponse)
async def kill_switch_status():
    """Read-only check — unlike /kill-switch/toggle, this never flips it."""
    return KillSwitchResponse(kill_switch_active=os.path.exists(KILL_SWITCH_PATH))


@router.post("/kill-switch/toggle", response_model=KillSwitchResponse)
async def toggle_kill_switch(action: str = Query(..., pattern="^(ACTIVATE|DEACTIVATE)$")):
    """ACTIVATE creates KILL_SWITCH.txt (blocks /run-daily-cycle and the
    scheduler, and forces execution_risk_agent to a HALTED state);
    DEACTIVATE removes it."""

    if action == "ACTIVATE":
        with open(KILL_SWITCH_PATH, "w") as f:
            f.write("Trading halted via /trading/kill-switch/toggle\n")
    else:
        if os.path.exists(KILL_SWITCH_PATH):
            os.remove(KILL_SWITCH_PATH)

    return KillSwitchResponse(kill_switch_active=os.path.exists(KILL_SWITCH_PATH))
