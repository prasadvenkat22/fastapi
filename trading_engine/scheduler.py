"""Optional background loop that repeatedly runs the same trading-cycle
logic as POST /trading/run-daily-cycle, so an open position's take-profit/
stop-loss/force-close rules actually get re-checked throughout the day
instead of only when someone manually hits the endpoint.

Never starts on its own — only via POST /trading/scheduler/start, and only
runs until POST /trading/scheduler/stop or the app restarts."""

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


def start(interval_seconds: int = 60) -> bool:
    """Returns False if already running (no-op), True if it just started."""
    global _task, _interval_seconds
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
