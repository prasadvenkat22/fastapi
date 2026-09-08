import os
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from config.db_pgrs import SessionLocal
from models_pgdb.trading_models import OpenPosition, TradeHistory, TradingLog
from schemas_pgrs.trading_schema import (
    BrokerPosition,
    BrokerPositionsResponse,
    KillSwitchResponse,
    OpenPositionResponse,
    PlaybookPerformanceResponse,
    PlaybookStat,
    SchedulerStatusResponse,
    TradeHistoryResponse,
    TradingCycleResponse,
    TradingStatusResponse,
)
from trading_engine import orphans, scheduler, tradier_orders
from trading_engine.broker import estimate_credit_value, estimate_spread_value, fill_price, is_credit
from trading_engine.playbook import WINDOWS
from trading_engine.data_feed import TradierDataError, fetch_qqq_spot
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

    # Must price exactly as the engine does, or this endpoint reports a P&L
    # the rules will never act on. It previously used intrinsic value alone,
    # which ignores the time value still in a spread hours from expiry --
    # observed reporting +47% on a position the engine marked at +5.8%.
    spot = fetch_qqq_spot()
    if is_credit(row.strategy):
        current_value = fill_price(
            estimate_credit_value(row.strategy, row.short_strike, row.long_strike, spot), "buy")
        per_spread = row.entry_net_debit - current_value   # credit profits as it decays
    else:
        current_value = fill_price(
            estimate_spread_value(row.strategy, row.long_strike, row.short_strike, spot), "sell")
        per_spread = current_value - row.entry_net_debit
    unrealized_pct = round((per_spread / row.entry_net_debit) * 100, 2)
    unrealized_dollars = round(per_spread * row.quantity * 100, 2)

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


@router.get("/positions", response_model=BrokerPositionsResponse)
async def get_broker_positions():
    """EVERYTHING the broker holds, not just what the engine opened.

    /position above reports the engine's own OpenPosition row and knows
    nothing about a position a human opened. On 2026-09-03 it would have shown
    nothing while seven manual structures were live and being managed.

    This reads the same reconstruction orphans.py runs every cycle: legs
    paired by the ORDER that opened them rather than guessed by strike, and
    entry taken from the fills rather than from Tradier's cost_basis, which
    averages across contracts bought and sold and drifted 3540 -> 4251.92 on
    an unchanged quantity of 5.

    Read-only. Nothing here places, closes or modifies anything -- the exit
    rules run in the cycle, not in a request handler, and a dashboard that can
    trade is a dashboard that will.
    """
    try:
        structures = orphans.open_structures()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Broker positions unavailable: {exc}")

    state = orphans._load()
    peaks = state.get("peaks") or {}
    out, total = [], 0.0
    for st in structures:
        mark = orphans._mark(st)
        value = ret = None
        if mark is not None:
            value, ret = mark
            entry = abs(st["entry"])
            per = ((entry - value) if st["credit"] else (value - entry))
            total += per * st["qty"] * 100

        rec = peaks.get(st["key"]) or {}
        peak = rec.get("peak")
        quiet = None
        if rec.get("peak_at"):
            try:
                quiet = round((datetime.now(timezone.utc)
                               - datetime.fromisoformat(rec["peak_at"])).total_seconds() / 60.0, 1)
            except Exception:
                quiet = None

        today = orphans._expires_today(st)
        zero_dte = (not orphans.ORPHAN_TODAY_ONLY) or today
        max_ret = orphans._max_return_pct(st)
        entry = abs(st["entry"])
        ceiling_value = (
            entry * (1 + orphans.ORPHAN_CEILING_FRACTION * max_ret / 100.0)
            if (max_ret and not st["credit"]) else None
        )
        managed = (orphans.MANAGE_ORPHANS
                   and (not orphans.MANAGE_UNDERLYING
                        or st["root"] in orphans.MANAGE_UNDERLYING))
        # Say which stall governs this one rather than printing both, for the
        # same reason the log line was fixed: a field that names a rule which
        # cannot fire is worse than no field.
        if zero_dte:
            giveback, armed = orphans.STALL_GIVEBACK_PCT, (peak or 0) > 0
        else:
            giveback = orphans.ORPHAN_LATER_STALL_GIVEBACK_PCT
            armed = peak is not None and peak >= orphans.ORPHAN_LATER_STALL_ARM_PCT

        out.append(BrokerPosition(
            underlying=st["root"], right=st["right"],
            long_strike=st["long_strike"], short_strike=st["short_strike"],
            quantity=st["qty"], expiry=st["expiry"], credit=st["credit"],
            entry=round(entry, 4),
            current_value=round(value, 4) if value is not None else None,
            intrinsic=(orphans._decompose(st, value) or (None, None))[0]
            if value is not None else None,
            extrinsic=(orphans._decompose(st, value) or (None, None))[1]
            if value is not None else None,
            return_pct=round(ret, 2) if ret is not None else None,
            peak_pct=round(peak, 2) if peak is not None else None,
            minutes_since_peak=quiet,
            ceiling_value=round(ceiling_value, 2) if ceiling_value else None,
            stop_pct=((orphans.ORPHAN_CREDIT_STOP_PCT if st["credit"]
                       else orphans.ORPHAN_STOP_PCT) if zero_dte else None),
            stall_giveback_pct=giveback,
            stall_armed=armed,
            expires_today=today,
            managed=managed,
            quote_tradeable=orphans._quotes_tradeable(
                tradier_orders.quotes([st["long"], st["short"]]), st),
            note=None if zero_dte else "ceiling and stall only; stop and force close are 0DTE rules",
        ))

    return BrokerPositionsResponse(
        positions=out, count=len(out),
        managed_underlyings=sorted(orphans.MANAGE_UNDERLYING),
        total_unrealized_dollars=round(total, 2) if out else None,
    )


@router.get("/history", response_model=TradeHistoryResponse)
async def get_trade_history(db: db_dependency):
    """Realized P&L from every closed trade, oldest to newest, plus the
    running total — this is where a closed position's profit or loss
    actually lives once its OpenPosition row is gone."""

    rows = db.query(TradeHistory).order_by(TradeHistory.closed_at.asc()).all()
    total = round(sum(r.realized_pnl_dollars for r in rows), 2)
    return TradeHistoryResponse(total_realized_pnl_dollars=total, trade_count=len(rows), trades=rows)


@router.get("/playbook-performance", response_model=PlaybookPerformanceResponse)
async def playbook_performance(db: db_dependency):
    """Realized results broken down by named entry strategy.

    Strike placement is chosen by time of day, so a day can produce trades
    from several strategies with genuinely different risk profiles � an ATM
    momentum spread and a midday ITM grinder are not the same bet. Pooling
    their P&L hides which one is actually working. This is the view that
    tells you which windows to keep and which to delete from playbook.WINDOWS.

    Strategies with no closed trades still appear, so an untested one reads
    as untested rather than silently missing.
    """
    rows = db.query(TradeHistory).all()

    by_name: dict[str, list] = {w.name: [] for w in WINDOWS}
    unattributed = 0
    for r in rows:
        if not r.playbook:
            unattributed += 1
            continue
        by_name.setdefault(r.playbook, []).append(r)

    active = {w.name for w in WINDOWS}
    meta = {w.name: w for w in WINDOWS}

    stats = []
    for name, trades in by_name.items():
        w = meta.get(name)
        wins = [t for t in trades if t.realized_pnl_dollars > 0]
        losses = [t for t in trades if t.realized_pnl_dollars <= 0]
        reasons: dict[str, int] = {}
        for t in trades:
            reasons[t.close_reason] = reasons.get(t.close_reason, 0) + 1
        pcts = [t.realized_pnl_pct for t in trades]
        stats.append(PlaybookStat(
            playbook=name,
            trades=len(trades),
            wins=len(wins),
            losses=len(losses),
            win_rate_pct=round(len(wins) / len(trades) * 100, 1) if trades else 0.0,
            total_pnl_dollars=round(sum(t.realized_pnl_dollars for t in trades), 2),
            avg_pnl_pct=round(sum(pcts) / len(pcts), 2) if pcts else 0.0,
            best_pct=round(max(pcts), 2) if pcts else 0.0,
            worst_pct=round(min(pcts), 2) if pcts else 0.0,
            close_reasons=reasons,
            active=name in active,
            window=f"{w.start.strftime('%H:%M')}-{w.end.strftime('%H:%M')} ET" if w else None,
            placement=w.placement if w else None,
        ))

    # Most traded first; untested strategies sort to the bottom.
    stats.sort(key=lambda s: (-s.trades, s.playbook))
    return PlaybookPerformanceResponse(stats=stats, unattributed_trades=unattributed)


@router.post("/scheduler/start", response_model=SchedulerStatusResponse)
async def start_scheduler(interval_minutes: int = Query(1, ge=1, le=60)):
    """Starts the background loop that re-runs the trading cycle on an
    interval during market hours (9:30AM-4:00PM EST, weekdays) — never
    starts on its own, only via this endpoint. Calling it again while
    already running just reports the current status."""

    try:
        scheduler.start(interval_seconds=interval_minutes * 60)
    except scheduler.SchedulerDisabled as exc:
        # 409, not 500: the request is well formed and the server is healthy,
        # it is the state that forbids it. See trading_engine/scheduler.py.
        raise HTTPException(status_code=409, detail=str(exc))
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


# ---------------------------------------------------------------------------
# SCREENER. Read-only, and slow by the standards of this router: each call
# fetches daily bars, an option chain and intraday bars per symbol, so a six
# name screen takes seconds rather than milliseconds. That is why `symbols` is
# capped -- a UI that lets someone paste forty tickers would hang the worker
# and spend the data budget on one request.
#
# NOTHING HERE TRADES. It ranks and returns; every route below is a GET.
# ---------------------------------------------------------------------------

MAX_SCREEN_SYMBOLS = 12


def _symbols(raw: str) -> list:
    syms = [s.strip().upper() for s in raw.split(",") if s.strip()]
    if not syms:
        raise HTTPException(status_code=422, detail="no symbols given")
    if len(syms) > MAX_SCREEN_SYMBOLS:
        raise HTTPException(
            status_code=422,
            detail=f"at most {MAX_SCREEN_SYMBOLS} symbols per call; "
                   f"each one costs a chain fetch")
    return syms


@router.get("/screener/verticals")
async def screen_verticals(
    symbols: str = Query(..., description="comma separated, e.g. SNDK,NVDA,CRWV"),
    side: str = Query("call", pattern="^(call|put)$"),
    by: str = Query("edge", pattern="^(edge|ev|evpct|prob)$"),
    top: int = Query(10, ge=1, le=100),
    rr_min: float = Query(0.0, ge=0.0),
    rr_max: float = Query(0.0, ge=0.0),
):
    """Rank debit verticals. Same maths as scripts/weekly_pick.py, one import.

    `by` defaults to EDGE rather than the CLI's evpct, because a UI shows the
    first row hardest and the other three sorts each put a structure nobody
    should take at the top: `prob` finds deep-ITM verticals whose reward is
    already spent (AVGO 345/358 asked 1250 to make nothing, break-even 100%),
    `evpct` finds the OTM lottery ticket (SNDK 2100/2200 at 1:39 on a 6.7%
    chance of any profit). Edge is Pwin minus the break-even win rate the
    price demands -- whether you are PAID for the odds.

    news and flow are RETURNED BUT NOT USED in the ranking. Neither has been
    scored against outcomes; both sit beside the decision (sections 22, 130).
    """
    from trading_engine.screener import rank

    try:
        out = rank(_symbols(symbols), side, by=by, top=top,
                   rr_min=rr_min, rr_max=rr_max)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"screen failed: {exc}")

    rows = []
    for r in out["rows"]:
        flow = r.get("flow") or {}
        rows.append({
            "symbol": r["sym"], "long_strike": r["lo"], "short_strike": r["hi"],
            "width": r["w"], "expiry": r["exp"], "days": r["days"],
            "itm_atr": round(r["itm"], 4),
            "risk": round(r["cost"] * 100, 2),
            "reward": round((r["w"] - r["cost"]) * 100, 2),
            "rr": round(r["rr"], 4),
            "p_imp": round(r["d_short"], 4), "p_hist": round(r["p_max"], 4),
            "p_mc": round(r["mc_max"], 4), "p_win": round(r["pwin"], 4),
            "need": round(r["need"], 4),
            "edge": round(r["pwin"] - r["need"], 4),
            "ev": round(r["ev_dem"], 2), "ev_adj": round(r["ev_adj"], 2),
            "ev_raw": round(r["ev_raw"], 2),
            "news": r.get("news"),
            "news_conflict": r.get("conflict"),
            "flow": flow.get("label"),
            "flow_up_pct": (round(flow["up_pct"], 2) if flow else None),
            "flow_conflict": r.get("flow_conflict"),
        })
    return {
        "side": out["side"], "sort": out["sort"],
        "sort_label": out["sort_label"],
        "considered": out["considered"], "returned": len(rows),
        "sorts_available": ["edge", "ev", "evpct", "prob"],
        "underlyings": out["meta"], "warnings": out["warnings"],
        "rows": rows,
        "note": ("news and flow are shown, not used. Ranking is EV and "
                 "probability only."),
    }


@router.get("/screener/flow")
async def screen_flow(
    symbols: str = Query(..., description="comma separated"),
    day: str = Query("", description="YYYY-MM-DD, default today (ET)"),
    interval: str = Query("5min", pattern="^(1min|5min|15min)$"),
):
    """Net signed volume and VWAP per symbol -- the cross-section.

    BUY and SELL require price-vs-VWAP AND the up-volume share to agree;
    anything else is MIXED. Signed volume alone called SNDK bought on
    2026-09-08 at 74% up-volume with price below a flat VWAP (section 129),
    so a client showing one number without the other will mislead.
    """
    from trading_engine.screener import flow_table

    try:
        rows = flow_table(_symbols(symbols), day=day, interval=interval)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"flow failed: {exc}")
    return {"day": day or "today", "interval": interval,
            "returned": len(rows), "rows": rows,
            "note": ("label requires price-vs-VWAP and up-volume share to "
                     "agree; MIXED means they do not. Measures urgency, not "
                     "institutional participation.")}
