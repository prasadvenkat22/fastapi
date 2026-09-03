"""Optional background loop that repeatedly runs the same trading-cycle
logic as POST /trading/run-daily-cycle, so an open position's take-profit/
stop-loss/force-close rules actually get re-checked throughout the day
instead of only when someone manually hits the endpoint.

Never starts on its own — only via POST /trading/scheduler/start, and only
runs until POST /trading/scheduler/stop or the app restarts.

DISABLED BY DEFAULT SINCE 2026-09-03, AND THE REASON MATTERS.

A cron entry already runs scripts/run_cycle.py every minute. This loop does
the SAME JOB. Run both and every cycle happens twice, concurrently, with no
lock between them — and two cycles that both see the same position can both
act on it. On 2026-09-03 a QQQ 714/716 ten-lot closed as two separate
five-lot orders rather than one, which is exactly the shape a race produces;
not proven, but it cost an hour of untangling TradeHistory rows that looked
like duplicates and were not (see sections 64 and 65).

The failure is also invisible while it is happening. Both runners log the
same lines to different places — cron to /var/log/qqq-trading.log, this loop
to the container log — so nothing in either says "something else is also
trading". It was only found by counting cycles per minute.

TRADING_ALLOW_APP_SCHEDULER=true re-enables it. Set that ONLY when the cron
entry has been removed; the two are alternatives, not layers."""

import asyncio
import logging
import os
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from config.db_pgrs import SessionLocal

from .nodes import KILL_SWITCH_PATH
from .service import execute_and_persist_cycle

logger = logging.getLogger(__name__)

MARKET_OPEN = (9, 30)   # EST
MARKET_CLOSE = (16, 0)  # EST

_task: Optional[asyncio.Task] = None
# 60s, not 300s. QQQ offers roughly three tradeable five-minute moves in a
# session, and at a 5-minute cadence a move that begins and ends inside one
# bar is invisible. The expensive half of the macro read is cached on its own
# clock (nodes.MACRO_REFRESH_MINUTES) so the faster cadence does not multiply
# the Claude and RSS cost.
_interval_seconds: int = 60


def is_running() -> bool:
    return _task is not None and not _task.done()


def get_interval_seconds() -> int:
    return _interval_seconds


def _within_market_hours() -> bool:
    now = datetime.now(ZoneInfo("America/New_York"))
    if now.weekday() >= 5:  # Saturday/Sunday
        return False
    now_hm = (now.hour, now.minute)
    return MARKET_OPEN <= now_hm < MARKET_CLOSE


async def _loop():
    while True:
        try:
            if os.path.exists(KILL_SWITCH_PATH):
                logger.info("Scheduler tick skipped — kill switch active.")
            elif not _within_market_hours():
                logger.info("Scheduler tick skipped — outside market hours.")
            else:
                db = SessionLocal()
                try:
                    final_state = await execute_and_persist_cycle(db)
                    logger.info("Scheduler cycle result: %s", final_state.get("execution_status"))
                finally:
                    db.close()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Scheduler cycle failed")
        await asyncio.sleep(_interval_seconds)


ALLOW_APP_SCHEDULER = os.getenv("TRADING_ALLOW_APP_SCHEDULER", "false").lower() == "true"


class SchedulerDisabled(RuntimeError):
    """Raised when start() is called while cron is the designated runner."""


def start(interval_seconds: int = 60) -> bool:
    """Returns False if already running (no-op), True if it just started.

    Raises SchedulerDisabled when TRADING_ALLOW_APP_SCHEDULER is not set --
    refusing loudly rather than starting a second concurrent runner. A silent
    no-op would be worse: the caller would believe the scheduler was running
    and cron would be doing the work, which is the same confusion that started
    this.
    """
    global _task, _interval_seconds
    if not ALLOW_APP_SCHEDULER:
        raise SchedulerDisabled(
            "The in-app scheduler is disabled: cron already runs the cycle every "
            "minute and running both means two concurrent cycles with no lock "
            "between them. Set TRADING_ALLOW_APP_SCHEDULER=true only after "
            "removing the cron entry."
        )
    if is_running():
        return False
    _interval_seconds = interval_seconds
    _task = asyncio.create_task(_loop())
    return True


def stop() -> bool:
    """Returns False if it wasn't running, True if it was just stopped."""
    global _task
    if not is_running():
        return False
    _task.cancel()
    _task = None
    return True
