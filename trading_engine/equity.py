"""Realized equity and the daily loss circuit breaker.

TRADING_POSITION_BUDGET is a static environment variable, so sizing read from
it directly never changed no matter what the account had actually done. Five
straight losses and the engine still deployed the same dollars against a
balance that no longer existed — position size stayed flat while capital
drained. There was no equity curve anywhere in the system.

This computes one from TradeHistory: starting budget plus every realized
result. Sizing then follows the account down after losses and up after wins,
which is the difference between a paper engine and something that can be
pointed at real money.

The daily loss limit is the other half. Because only one position is open at
a time and the worst case on a force-closed loser is everything deployed, a
bad day can otherwise repeat until the session ends. Past the limit, new
entries stop for the rest of the day — open positions are still managed,
since refusing to manage a position you already hold is not risk control.
"""

import logging
import os
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from config.db_pgrs import SessionLocal
from models_pgdb.trading_models import TradeHistory

logger = logging.getLogger(__name__)

NY = ZoneInfo("America/New_York")

# Halt new entries once the day's realized losses reach this share of the
# equity the session started with. 0.25 gives roughly two full stop-outs at
# the default entry fraction before the engine stands down.
MAX_DAILY_LOSS_PCT = float(os.getenv("TRADING_MAX_DAILY_LOSS_PCT", "0.02"))

# After a losing exit, refuse the SAME direction for this many minutes.
#
# Without it the engine re-enters the setup it was just stopped out of, on the
# very next cycle, because the signals still read the same -- observed live
# taking three put spreads in nineteen minutes, two of them already closed at
# a loss, each paying the bid-ask both ways. Repeating a trade the market has
# just rejected is not a new opportunity.
#
# 30 rather than the original 20, which was picked rather than measured.
# Simulated over 59 sessions on the deployed config, varying only this:
#
#     0 min   +51.17 a day   158 trades   worst day -259
#    20 min   +55.03 a day   154 trades   worst day -241
#    30 min   +58.13 a day   149 trades   worst day -241
#    45 min   +58.44 a day   150 trades   worst day -241
#    90 min   +60.15 a day   143 trades   worst day -241
#
# Monotonic: every extra shot after a loss costs money on average. Removing
# the cooldown entirely gives up 228 dollars over the sample AND widens the
# worst day. 30 takes most of the available gain; past 45 the curve is nearly
# flat and the sample thins, so there is no case for going further on this
# evidence.
REENTRY_COOLDOWN_MINUTES = float(os.getenv("TRADING_REENTRY_COOLDOWN_MINUTES", "30"))

# Consecutive losing trades before entries stop for the session. The daily
# loss limit counts dollars; this counts being wrong repeatedly, which can
# happen well before the dollar limit on a day the strategy simply does not
# fit the tape.
MAX_CONSECUTIVE_LOSSES = int(os.getenv("TRADING_MAX_CONSECUTIVE_LOSSES", "3"))

BULLISH_STRATEGIES = ("BULL_CALL_SPREAD", "PUT_CREDIT_SPREAD")


@dataclass
class EquityState:
    starting_budget: float     # TRADING_POSITION_BUDGET
    realized_total: float      # all-time realized P&L
    equity: float              # starting_budget + realized_total
    realized_today: float      # today's realized P&L (negative = losing)
    daily_loss_limit: float    # dollars, as a positive number
    halted: bool               # today's losses have reached the limit

    @property
    def session_start_equity(self) -> float:
        """Equity at the start of today, i.e. before today's results."""
        return self.equity - self.realized_today


def blocked_direction() -> "str | None":
    """'bullish', 'bearish', or None — the side to refuse right now.

    Looks only at the most recent closed trade: if it lost and closed inside
    the cooldown, that direction is off the table. A winning exit imposes no
    cooldown, since the setup just worked.
    """
    try:
        db = SessionLocal()
        try:
            row = (
                db.query(TradeHistory.strategy, TradeHistory.realized_pnl_dollars, TradeHistory.closed_at)
                .order_by(TradeHistory.closed_at.desc())
                .first()
            )
            if row is None or row[1] is None or row[1] >= 0 or row[2] is None:
                return None
            from datetime import timezone
            age_min = (datetime.now(timezone.utc) - row[2]).total_seconds() / 60.0
            if age_min >= REENTRY_COOLDOWN_MINUTES:
                return None
            direction = "bullish" if row[0] in BULLISH_STRATEGIES else "bearish"
            logger.info(
                "Re-entry cooldown: last %s trade lost %.2f, %.1f min ago — that side is blocked for %.0f min.",
                direction, row[1], age_min, REENTRY_COOLDOWN_MINUTES,
            )
            return direction
        finally:
            db.close()
    except Exception:
        logger.exception("Cooldown check failed — not blocking.")
        return None


def consecutive_losses_today() -> int:
    """How many of today's most recent closed trades lost, in a row."""
    try:
        db = SessionLocal()
        try:
            rows = (
                db.query(TradeHistory.realized_pnl_dollars)
                .filter(TradeHistory.closed_at >= _today_start())
                .order_by(TradeHistory.closed_at.desc())
                .all()
            )
            n = 0
            for (pnl,) in rows:
                if pnl is not None and pnl < 0:
                    n += 1
                else:
                    break
            return n
        finally:
            db.close()
    except Exception:
        logger.exception("Consecutive-loss check failed — reporting zero.")
        return 0


def _today_start() -> datetime:
    return datetime.now(NY).replace(hour=0, minute=0, second=0, microsecond=0)


def current_equity(starting_budget: float) -> EquityState:
    """Equity and circuit-breaker state, read from realized trade history.

    Best-effort: if the query fails the caller still gets a usable state built
    from the starting budget alone, so a database hiccup degrades sizing to
    the old static behaviour rather than halting trading outright.
    """
    realized_total = 0.0
    realized_today = 0.0

    try:
        db = SessionLocal()
        try:
            rows = db.query(TradeHistory.realized_pnl_dollars, TradeHistory.closed_at).all()
            start = _today_start()
            for pnl, closed_at in rows:
                pnl = pnl or 0.0
                realized_total += pnl
                if closed_at is not None and closed_at >= start:
                    realized_today += pnl
        finally:
            db.close()
    except Exception:
        logger.exception("Equity history unavailable — falling back to the static budget for sizing.")

    equity = starting_budget + realized_total
    session_start = equity - realized_today
    limit = max(session_start, 0.0) * MAX_DAILY_LOSS_PCT
    halted = limit > 0 and realized_today <= -limit

    if halted:
        logger.warning(
            "Daily loss limit reached: %.2f lost today against a %.2f limit (%.0f%% of session-start equity "
            "%.2f) — no new entries for the rest of the session.",
            realized_today, limit, MAX_DAILY_LOSS_PCT * 100, session_start,
        )

    return EquityState(
        starting_budget=starting_budget,
        realized_total=round(realized_total, 2),
        equity=round(equity, 2),
        realized_today=round(realized_today, 2),
        daily_loss_limit=round(limit, 2),
        halted=halted,
    )
