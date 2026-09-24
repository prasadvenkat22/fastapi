import math
import os
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

import models_pgdb.models as models
from config.db_pgrs import SessionLocal
from helpers.auth_deps import require_admin
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
from trading_engine import orphans, scheduler, settings_overrides, tradier_orders
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


def _week_vwap_fields(flow: "dict | None", direction: "str | None") -> dict:
    """The board's Week VWAP column for one row, from a weekly_vwap_gate read.

    `week_vwap_trend` is LONG / SHORT / MIXED / None; `week_vwap_conflict`
    names a row whose direction leans against it (a bullish row on a SHORT
    week, a bearish one on a LONG week). MIXED conflicts with nothing: it says
    the week has no lean, which is information, not a veto.
    """
    from trading_engine.weekly_vwap_gate import trend_label
    trend = trend_label(flow)
    conflict = None
    if trend == "SHORT" and direction == "bullish":
        conflict = "bullish structure on a week whose volume is paying down"
    elif trend == "LONG" and direction == "bearish":
        conflict = "bearish structure on a week whose volume is paying up"
    return {
        "week_vwap": (flow["vwap_week"] if flow else None),
        "week_vwap_side": (flow["side"] if flow else None),
        "week_vwap_slope_pct": (flow["slope_pct"] if flow else None),
        "week_vwap_sessions": (flow["sessions"] if flow else None),
        "week_vwap_trend": trend,
        "week_vwap_conflict": conflict,
    }


def _vol_fields(meta: dict) -> dict:
    """The volatility regime for one underlying, from the screener's meta.

    `iv` is the ATM implied vol of the screened expiry, `rv` the 20-day
    realised (both annualised, from weekly_pick.evaluate). `iv_rv` above 1
    means the options are priced richer than the name has been moving --
    the regime that favours SELLING spreads; below 1 favours buying them
    (section 198 read 0.48-0.83 across the names and chose debits; section
    213 scored the credit shadow by this ratio). Shown, not ranked on.
    """
    iv, rv = meta.get("iv"), meta.get("rv")
    ratio = (iv / rv) if (iv and rv and rv > 0 and math.isfinite(iv) and math.isfinite(rv)) else None
    if ratio is None:
        regime = None
    elif ratio >= 1.2:
        regime = "RICH"
    elif ratio <= 0.8:
        regime = "CHEAP"
    else:
        regime = "FAIR"
    return {"iv": (round(iv, 4) if iv is not None else None),
            "rv": (round(rv, 4) if rv is not None else None),
            "iv_rv": (round(ratio, 3) if ratio is not None else None),
            "vol_regime": regime}


def _json_safe(obj: Any) -> Any:
    """NaN and +/-inf become null, recursively.

    Python's json module refuses them ("Out of range float values are not
    JSON compliant") and FastAPI turns that into a 500 with no body, which is
    what the screener board showed on 2026-09-21 before the open. The maths
    behind these endpoints legitimately produces NaN when an input is missing
    -- an ATM IV with no quoted vols, a Monte Carlo band with no ATR -- and a
    null says "not measured" where a 500 says nothing at all.
    """
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    return obj


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
        peak_at = rec.get("peak_at")
        if orphans.STALL_ON_MARK and orphans._expires_today(st):
            # Section 231: the stall watches the sale price; show its peak.
            peak = orphans.mark_peak(rec, abs(st["entry"]), st["credit"])
            peak_at = rec.get("mpeak_at")
        quiet = None
        if peak_at:
            try:
                quiet = round((datetime.now(timezone.utc)
                               - datetime.fromisoformat(peak_at)).total_seconds() / 60.0, 1)
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
        # ASK THE ENGINE'S OWN FUNCTION, never a raw setting. Reading the knob
        # reported 40.0 on a weekly whose real threshold was 9.1 points, and
        # 20.0 on a 0DTE whose real one was 12.3 -- both the flat fallbacks
        # that ORPHAN_STALL_GIVEBACK_BAND overrides. Section 131 again: a value
        # computed twice drifts.
        width = abs(st["short_strike"] - st["long_strike"])
        if zero_dte:
            band = orphans.ORPHAN_STALL_GIVEBACK_BAND
            flat = orphans.STALL_GIVEBACK_PCT
            quiet_needed = orphans.STALL_MINUTES
            armed = orphans.stall_arm_reached(peak)
        else:
            band = orphans.ORPHAN_LATER_STALL_GIVEBACK_BAND
            flat = None
            LP = orphans.later_params(st)         # scaled by sessions left (§199)
            quiet_needed = LP["stall_minutes"]
            armed = peak is not None and peak >= LP["stall_arm"]
        giveback = orphans._giveback_points(
            st["root"], entry, peak or 0.0, flat, width, band,
            None if zero_dte else LP["giveback_atr"])

        # The drag ceiling decides whether a PROFITABLE exit is allowed at all,
        # and it was the missing field: it is what held this position open.
        drag_ceiling = width * orphans.ORPHAN_MAX_DRAG_WIDTH
        iv_now = (orphans._decompose(st, value) or (None, None))[0] if value is not None else None
        drag_now = round(iv_now - value, 4) if (iv_now is not None and value is not None) else None
        drag_blocks = (orphans.ORPHAN_MAX_DRAG_WIDTH > 0 and drag_now is not None
                       and width > 0 and drag_now > drag_ceiling)

        _hold = (orphans.ORPHAN_HOLD_UNTIL if zero_dte
                 else (orphans.ORPHAN_LATER_HOLD_UNTIL or orphans.ORPHAN_HOLD_UNTIL))
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
            # A WEEKLY HAS A STOP. The old version returned null here and a
            # note saying the stop was 0DTE-only, which was true before
            # ORPHAN_LATER_STOP_PCT existed and has been wrong since.
            stop_pct=((orphans.ORPHAN_CREDIT_STOP_PCT if st["credit"]
                       else orphans.ORPHAN_STOP_PCT) if zero_dte
                      else (LP["stop_pct"] if LP["stop_pct"] < 0 else None)),
            stop_confirm_minutes=(orphans.ORPHAN_STOP_CONFIRM_MINUTES if zero_dte
                                  else LP["stop_minutes"]),
            stall_giveback_points=round(giveback, 2),
            stall_giveback_pct=round(giveback, 2),
            stall_quiet_minutes=quiet_needed,
            stall_armed=armed,
            stall_min_gain_pct=orphans.STALL_MIN_GAIN_PCT,
            drag_ceiling=round(drag_ceiling, 2),
            drag_now=drag_now,
            drag_blocks=drag_blocks,
            hold_until=_hold or None,
            past_hold=orphans._past_hold_until(_hold),
            expires_today=today,
            managed=managed,
            quote_tradeable=orphans._quotes_tradeable(
                tradier_orders.quotes([st["long"], st["short"]]), st),
            note=None if zero_dte else "no 15:45 flatten; runs to expiry",
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
# SETTINGS. The whitelisted exit/entry knobs, written to trading_overrides.env
# (see trading_engine/settings_overrides.py). Reading is open to the trading
# roles; writing is admin-only, because a stop-loss change moves real money
# on the next cron minute.
# ---------------------------------------------------------------------------

class SettingsUpdate(BaseModel):
    values: dict[str, str | float | int | bool] = Field(default_factory=dict)
    unset: list[str] = Field(default_factory=list)


@router.get("/settings")
async def get_settings():
    """Every tunable: code default, .env.production value, override, effective."""
    return settings_overrides.snapshot()


@router.put("/settings")
async def update_settings(body: SettingsUpdate,
                          user: Annotated[models.User, Depends(require_admin())]):
    """Validate everything first, then write once; a bad value changes nothing."""
    try:
        settings_overrides.unset(body.unset, who=user.email)
        values = {k: (str(v).lower() if isinstance(v, bool) else v) for k, v in body.values.items()}
        settings_overrides.set_many(values, who=user.email)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return settings_overrides.snapshot()


# ---------------------------------------------------------------------------
# MACRO PANEL. What the engine's risk gates see right now: VIX, crude and the
# 10Y against the session open and the thresholds that force risk-off, the
# rotation's QQQ macro verdict, and today's scheduled events and releases.
# Read-only; each reading fails soft to null rather than 500 the panel.
# ---------------------------------------------------------------------------

@router.get("/macro")
async def macro_panel(db: db_dependency):
    from trading_engine import macro_calendar, nodes
    from trading_engine.data_feed import fetch_oil, fetch_tnx, fetch_vix

    def _read(fn):
        try:
            return fn()
        except Exception:
            return None

    vix, oil, tnx = _read(fetch_vix), _read(fetch_oil), _read(fetch_tnx)
    gates = []
    if tnx is not None:
        gates.append({"name": "10Y spike", "value": f"{tnx.change_bps:+.1f}bp",
                      "limit": f">= +{nodes.TNX_SPIKE_BPS:g}bp",
                      "tripped": tnx.change_bps >= nodes.TNX_SPIKE_BPS})
    if vix is not None:
        gates.append({"name": "VIX level", "value": f"{vix.level:.2f}",
                      "limit": f">= {nodes.VIX_LEVEL_MAX:g}",
                      "tripped": vix.level >= nodes.VIX_LEVEL_MAX})
        gates.append({"name": "VIX spike", "value": f"{vix.change_pct:+.1f}%",
                      "limit": f">= +{nodes.VIX_SPIKE_PCT:g}%",
                      "tripped": vix.change_pct >= nodes.VIX_SPIKE_PCT})
    if oil is not None and nodes.CRUDE_SPIKE_PCT > 0:
        gates.append({"name": "Crude spike", "value": f"{oil.change_pct:+.2f}%",
                      "limit": f">= +{nodes.CRUDE_SPIKE_PCT:g}%",
                      "tripped": oil.change_pct >= nodes.CRUDE_SPIKE_PCT})

    rotation = None
    try:
        from trading_engine.symbol_news import classify_day
        g = classify_day("QQQ") or {}
        if g.get("verdict"):
            rotation = {"verdict": g.get("verdict"), "confidence": g.get("confidence")}
    except Exception:
        rotation = None

    last = db.query(TradingLog).order_by(TradingLog.timestamp.desc()).first()
    return _json_safe({
        "readings": {
            "vix": vars(vix) if vix else None,
            "crude": vars(oil) if oil else None,
            "tnx": vars(tnx) if tnx else None,
        },
        "gates": gates,
        "risk_off": any(x["tripped"] for x in gates),
        "engine": {"sentiment": getattr(last, "market_sentiment", None),
                   "status": getattr(last, "execution_status", None),
                   "at": getattr(last, "timestamp", None)},
        "rotation": rotation,
        "calendar": {"event_day": macro_calendar.is_event_day(),
                     "note": macro_calendar.describe() or None,
                     "releases": macro_calendar.releases_on()},
    })


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


def _direction(side: str, structure: str) -> str:
    """Mirrors weekly_pick.direction so the envelope states it once."""
    if structure == "credit":
        return "bearish" if side == "call" else "bullish"
    return "bullish" if side == "call" else "bearish"


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
    structure: str = Query("debit", pattern="^(debit|credit)$"),
    by: str = Query("edge", pattern="^(edge|ev|evpct|prob)$"),
    top: int = Query(10, ge=1, le=100),
    per_symbol: int = Query(0, ge=0, le=50),
    rr_min: float = Query(0.0, ge=0.0),
    rr_max: float = Query(0.0, ge=0.0),
    expiry: str = Query("", description="exact date (2026-09-21) or \"+N\" for the "
                                        "first expiry at least N days out; blank = "
                                        "each name's NEAREST, which on Monday is the "
                                        "same day for MU/NVDA/TSLA and Friday for "
                                        "SNDK -- ask for one date to compare a board"),
):
    """Rank verticals, bought or sold. Same maths as weekly_pick.py, one import.

    `structure=debit` is the BUY list, `structure=credit` the SELL list.
    THE DIRECTION FLIPS WITH IT, which is the thing to get right in a UI: a
    call DEBIT spread is bullish, a call CREDIT spread is bearish. Every row
    carries an explicit `direction` field so a client never has to infer it
    from `side`, and the news and flow conflict flags key on that field rather
    than on the option type.

    Credit rows report `risk` as width minus the credit and `reward` as the
    credit, so `rr`, `need` and `edge` mean the same thing in both lists and
    one sort works across them.

    `per_symbol` caps rows per name. Without it one symbol takes the page: a
    screen over CRWV, AVGO and SNDK on 2026-09-08 returned twelve CRWV rows
    and nothing else, because a single favourable IV/RV lifts every strike on
    that name above every strike on the others.

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
                   rr_min=rr_min, rr_max=rr_max,
                   structure=structure, per_symbol=per_symbol, expiry=expiry)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"screen failed: {exc}")

    # THE WEEK'S VWAP, ONE READ PER UNDERLYING -- section 212. The same
    # anchored read the weekly gate makes at entry (weekly_vwap_gate), so the
    # board's column and the gate's verdict cannot disagree. Shown, not used:
    # the ranking is still EV and probability.
    week = {}
    for m in out["meta"]:
        try:
            from trading_engine import weekly_vwap_gate
            week[m["symbol"]] = weekly_vwap_gate.read(m["symbol"], m.get("atr"))
        except Exception:
            week[m["symbol"]] = None
    for m in out["meta"]:
        m.update(_week_vwap_fields(week.get(m["symbol"]), None))
        m.update(_vol_fields(m))
    vol_by_sym = {m["symbol"]: _vol_fields(m) for m in out["meta"]}

    rows = []
    for r in out["rows"]:
        flow = r.get("flow") or {}
        rows.append({
            **_week_vwap_fields(week.get(r["sym"]), r.get("direction")),
            **vol_by_sym.get(r["sym"], _vol_fields({})),
            "symbol": r["sym"], "lower_strike": r["lo"], "upper_strike": r["hi"],
            "structure": r.get("structure"), "direction": r.get("direction"),
            "credit": (round(r["credit"], 4) if r.get("credit") else None),
            "width": r["w"], "expiry": r["exp"], "days": r["days"],
            "itm_atr": round(r["itm"], 4),
            "risk": round(r["cost"] * 100, 2),
            "reward": round((r["w"] - r["cost"]) * 100, 2),
            "rr": round(r["rr"], 4),
            # The chain's deltas, per leg and net (section 215). Shown for
            # comparison with Pwin; not an input to the ranking.
            "delta_long": (round(abs(r["d_long"]), 4) if r.get("d_long") is not None else None),
            "delta_short": (round(abs(r["d_short"]), 4) if r.get("d_short") is not None else None),
            "delta_net": (round(abs(r["d_net"]), 4) if r.get("d_net") is not None else None),
            "p_imp": round(r["p_imp"], 4), "p_hist": round(r["p_max"], 4),
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
    return _json_safe({
        "side": out["side"], "structure": out["structure"],
        "direction": _direction(side, structure),
        "sort": out["sort"],
        "sort_label": out["sort_label"],
        "considered": out["considered"], "returned": len(rows),
        "sorts_available": ["edge", "ev", "evpct", "prob"],
        "structures_available": ["debit", "credit"],
        "underlyings": out["meta"], "warnings": out["warnings"],
        "rows": rows,
        "note": ("news and flow are shown, not used. Ranking is EV and "
                 "probability only."),
    })


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
    return _json_safe({"day": day or "today", "interval": interval,
                       "returned": len(rows), "rows": rows,
                       "note": ("label requires price-vs-VWAP and up-volume share to "
                                "agree; MIXED means they do not. Measures urgency, not "
                                "institutional participation.")})


# ---------------------------------------------------------------------------
# FLATTEN. The panic button, and the only endpoint here that can lose money.
# ---------------------------------------------------------------------------

def _engine_legs() -> set:
    """OCC symbols the ENGINE owns, so a manual flatten never touches them.

    Built the same way service.py builds it for orphans.review(): the engine's
    own rows, its two strikes, today's expiry. Anything not in this set was
    opened by hand and is what /flatten is for.
    """
    out: set = set()
    try:
        from trading_engine.service import option_type_for, today_expiry

        db = SessionLocal()
        try:
            for row in db.query(OpenPosition).all():
                cp = option_type_for(row.strategy)
                for k in (row.long_strike, row.short_strike):
                    if k is None:
                        continue
                    out.add(tradier_orders.occ_symbol(
                        row.underlying or "QQQ", today_expiry(), cp, float(k)))
        finally:
            db.close()
    except Exception:
        # Fail CLOSED: if the engine's rows cannot be read we return an empty
        # set, which means flatten would include them. That is the wrong way
        # round for safety, so say so loudly rather than silently.
        import logging
        logging.getLogger(__name__).warning(
            "Could not read engine positions — /flatten may include them.",
            exc_info=True)
    return out


def _pair_leftovers(held: dict, want: str) -> list:
    """Pair whatever is STILL held into closable spreads, from positions alone.

    open_structures() reconstructs from ORDER HISTORY, and that endpoint
    returns a window measured in count rather than days -- so a spread whose
    opening order has aged out of it is not merely stale, it is INVISIBLE. A
    flatten built only on that would silently leave a real position open while
    reporting success, which is the one failure a panic button must not have.

    This is the backstop, and it needs no order history at all: match remaining
    longs to remaining shorts on the same root, expiry and right, lowest strike
    first. `held` carries SIGNED quantities, positive long and negative short,
    already decremented by whatever the structure pass claimed.
    """
    import re

    occ = re.compile(r"^([A-Z]+)(\d{6})([CP])(\d{8})$")
    longs: dict = {}
    shorts: dict = {}
    for sym, qty in held.items():
        if not qty:
            continue
        m = occ.match(str(sym).upper())
        if not m:
            continue
        root, ymd, cp, strike = m.groups()
        if want and root != want:
            continue
        bucket = longs if qty > 0 else shorts
        bucket.setdefault((root, ymd, cp), []).append(
            [int(strike) / 1000.0, sym, abs(int(qty))])
    out = []
    for key in sorted(set(longs) | set(shorts)):
        root, ymd, cp = key
        ls = sorted(longs.get(key, []))
        ss = sorted(shorts.get(key, []))
        li = si = 0
        while li < len(ls) and si < len(ss):
            n = min(ls[li][2], ss[si][2])
            if n <= 0:
                break
            out.append({
                "underlying": root,
                "expiry": f"20{ymd[:2]}-{ymd[2:4]}-{ymd[4:6]}",
                "right": "put" if cp == "P" else "call",
                "long_strike": ls[li][0], "short_strike": ss[si][0],
                "quantity": n, "long": ls[li][1], "short": ss[si][1],
                "source": "holdings",
            })
            ls[li][2] -= n
            ss[si][2] -= n
            held[ls[li][1]] = held.get(ls[li][1], 0) - n
            held[ss[si][1]] = held.get(ss[si][1], 0) + n
            if ls[li][2] == 0:
                li += 1
            if ss[si][2] == 0:
                si += 1
    return out


def _in_market_hours() -> bool:
    """Regular hours on a real trading day, holidays included."""
    from datetime import time as _t
    from zoneinfo import ZoneInfo

    from trading_engine import market_calendar

    now = datetime.now(ZoneInfo("America/New_York"))
    if not market_calendar.is_trading_day(now.date()):
        return False
    return _t(9, 30) <= now.time() <= _t(16, 0)


def _force_hours() -> bool:
    return os.getenv("TRADING_FLATTEN_IGNORE_HOURS", "").lower() == "true"


def _plan_token(plan: list) -> str:
    """A fingerprint of THIS plan, so execution cannot follow a stale preview.

    A boolean preview flag was not enough. On 2026-09-10 an instruction to
    "see if it works" was read as authorisation and four spreads were closed
    when a dry run was wanted -- one query parameter between looking and
    trading, with nothing tying the second call to the first.
    """
    import hashlib
    import json as _json

    body = _json.dumps(
        [[p["underlying"], p["long_strike"], p["short_strike"], p["quantity"],
          p.get("right"), p.get("expiry")] for p in plan],
        sort_keys=True)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]



@router.post("/flatten")
async def flatten_all(
    confirm: str = Query(..., description='must be exactly "LIQUIDATE"'),
    preview: bool = Query(True, description="true lists what it WOULD close"),
    underlying: str = Query("", description="limit to one symbol, blank = all"),
    plan_token: str = Query("", description="the token a preview returned; "
                                            "required when preview=false"),
):
    """Close every open spread at the broker. POST, and confirmed twice.

    THE KILL SWITCH DOES NOT DO THIS. KILL_SWITCH.txt halts algorithmic
    execution -- the engine stops deciding -- and leaves every position exactly
    where it is. That is the right behaviour for "stop trading" and the wrong
    one for "get me out", and the two were easy to confuse until they sat next
    to each other.

    IT CLOSES STRUCTURES, NOT LEGS. orphans.open_structures() supplies the
    pairing and each spread goes as ONE multileg order, because legging out is
    how a long turns into a naked short: sell the long, have the short leg's
    close rejected or unfilled, and an account that was risk-defined a second
    ago is now short a call with unbounded loss. Tradier fills a multileg order
    as a package or not at all, which is the property that matters here.

    ANY LEG THAT IS NOT PART OF A PAIR IS REPORTED AND NOT TRADED. A panic
    button that leaves something open is bad; one that legs into a naked short
    while panicking is far worse. The response names them so a human can act.

    PRICED AT THE NATURAL, never market. A two-leg market order in a thin chain
    is how a spread fills several dollars from its mid -- and this endpoint is
    most likely to be called exactly when the book is at its widest.

    preview=true by default. You have to ask for it twice: once with the
    confirm string, once by turning preview off.
    """
    if confirm != "LIQUIDATE":
        raise HTTPException(
            status_code=400,
            detail='refusing: pass confirm=LIQUIDATE to acknowledge this '
                   'closes real positions')

    # OUTSIDE MARKET HOURS THE QUOTES ARE FICTION AND SO IS THE PLAN.
    #
    # Measured 2026-09-10: the same four spreads previewed at $15,297 at 09:20
    # and $6,721 at 09:33. The pre-open figure was built from stale marks that
    # no longer existed by the opening print, and an operator reading it would
    # have been deciding on a number more than twice the real one. Worse, the
    # limits derived from it would have been sent into a book that had moved.
    if not (_force_hours() or _in_market_hours()):
        raise HTTPException(
            status_code=409,
            detail="refusing: outside regular trading hours. Quotes are stale "
                   "and any plan built from them misprices the position — a "
                   "preview at 09:20 read $15,297 for spreads worth $6,721 at "
                   "09:33. Try again between 09:30 and 16:00 ET.")

    from trading_engine.orphans import open_structures

    want = underlying.strip().upper()
    try:
        engine_owned = _engine_legs()
        structures = [st for st in (open_structures(engine_owned) or [])
                      if not want or st["root"] == want]
        legs = [p for p in (tradier_orders.open_positions() or [])
                if str(p.get("symbol")) not in engine_owned]
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"cannot read account: {exc}")

    def _iso(exp) -> str:
        """Structures carry %y%m%d ("260911"); occ_symbol wants %Y-%m-%d.

        Getting this wrong builds a symbol for a different contract, which is
        the single most expensive mistake this endpoint could make.
        """
        e = str(exp)
        return f"20{e[:2]}-{e[2:4]}-{e[4:6]}" if len(e) == 6 else e

    # WHAT THE ACCOUNT ACTUALLY HOLDS, per contract symbol. open_structures()
    # pairs by expiry and right, and when several long strikes back one short
    # strike it can report a quantity no single strike can fill.
    #
    # Measured 2026-09-10: legs were 1725 x2, 1750 x3, 1800 x-5, and the
    # pairing came back "SNDK 1750/1800 x5". Selling five 1750s when three are
    # held is rejected by the broker -- and a flatten that reports "submitted"
    # while the position is still open is worse than no flatten at all, since
    # the whole point is knowing you are out.
    held = {}
    for p in legs:
        try:
            held[str(p.get("symbol"))] = int(float(p.get("quantity") or 0))
        except Exception:
            continue

    paired = set()
    plan = []
    short_fall = []
    for st in structures:
        root = st["root"]
        lo, hi = st["long_strike"], st["short_strike"]
        qty = int(st["qty"])
        try:
            q = tradier_orders.quotes([st["long"], st["short"]])
            bid = float((q.get(st["long"]) or {}).get("bid") or 0)
            ask = float((q.get(st["short"]) or {}).get("ask") or 0)
            limit = round(bid - ask, 2)
        except Exception:
            limit = 0.0
        # Clamp to the smaller of the two legs actually on hand, and consume
        # it so a second structure cannot claim the same contracts.
        avail = int(min(abs(held.get(st["long"], 0)),
                        abs(held.get(st["short"], 0)), qty))
        if avail < qty:
            short_fall.append({
                "underlying": root, "long_strike": lo, "short_strike": hi,
                "pairing_said": qty, "actually_closable": avail,
                "why": "the account does not hold that many at both strikes",
            })
        if avail <= 0:
            continue
        held[st["long"]] = held.get(st["long"], 0) - avail     # long: toward 0
        held[st["short"]] = held.get(st["short"], 0) + avail    # short: toward 0
        qty = avail
        plan.append({
            "underlying": root, "long_strike": lo, "short_strike": hi,
            "quantity": qty, "expiry": _iso(st.get("expiry")),
            "right": "put" if str(st.get("right", "C")).upper().startswith("P")
                     else "call",
            "is_credit": bool(st.get("credit")),
            "entry": st.get("entry"),
            "limit": limit,
            "proceeds_estimate": round(limit * qty * 100, 2),
        })

    # THE BACKSTOP. Anything the order history did not account for is paired
    # here from holdings alone, so a spread whose opening order aged out of the
    # window is still closed rather than silently left open.
    for extra in _pair_leftovers(held, want):
        try:
            q2 = tradier_orders.quotes([extra["long"], extra["short"]])
            bid = float((q2.get(extra["long"]) or {}).get("bid") or 0)
            ask = float((q2.get(extra["short"]) or {}).get("ask") or 0)
            lim = round(bid - ask, 2)
        except Exception:
            lim = 0.0
        plan.append({
            "underlying": extra["underlying"],
            "long_strike": extra["long_strike"],
            "short_strike": extra["short_strike"],
            "quantity": extra["quantity"], "expiry": extra["expiry"],
            "right": extra["right"], "is_credit": False, "entry": None,
            "limit": lim,
            "proceeds_estimate": round(lim * extra["quantity"] * 100, 2),
            "source": "holdings (not in order history)",
        })

    # Anything still on hand after both passes is genuinely unpaired.
    orphan_legs = [
        {"symbol": p.get("symbol"), "quantity": p.get("quantity"),
         "remaining_after_plan": held.get(str(p.get("symbol")), 0),
         "cost_basis": p.get("cost_basis")}
        for p in legs
        if held.get(str(p.get("symbol")), 0) != 0
        and (not want or str(p.get("symbol", "")).upper().startswith(want))
    ]

    token = _plan_token(plan)
    if preview:
        return {
            "preview": True, "would_close": plan,
            "pairing_shortfalls": short_fall,
            "unpaired_legs_NOT_closed": orphan_legs,
            "plan_token": token,
            "to_execute": (f"repeat with preview=false&plan_token={token}"
                           if plan else "nothing to close"),
            "note": ("NOTHING WAS SENT. Execution requires this exact token, "
                     "so it can only follow a preview of this exact plan — if "
                     "the market moves and the plan changes, the token stops "
                     "matching and you get a fresh preview instead of a "
                     "surprise fill. Unpaired legs are never traded here: "
                     "closing one leg of a spread can leave a naked short."),
        }

    if plan_token != token:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "plan_token missing or stale — nothing was sent",
                "why": ("Execution must follow a preview of the SAME plan. "
                        "Either you have not previewed, or the market moved "
                        "and the plan is no longer what you looked at."),
                "current_token": token,
                "would_close": plan,
            })

    sent, failed = [], []
    for p in plan:
        try:
            res = tradier_orders.submit_vertical(
                p["underlying"], p["expiry"], p["right"],
                long_strike=p["long_strike"], short_strike=p["short_strike"],
                quantity=p["quantity"], opening=False,
                limit_price=abs(p["limit"]), is_credit=p["is_credit"],
                preview=False)
            sent.append({**p, "order": res})
        except Exception as exc:
            failed.append({**p, "error": str(exc)})
    return {"preview": False, "submitted": sent, "failed": failed,
            "pairing_shortfalls": short_fall,
            "unpaired_legs_NOT_closed": orphan_legs,
            "note": ("orders are LIMIT at the natural and may not fill. Check "
                     "/trading/positions before assuming you are flat.")}
