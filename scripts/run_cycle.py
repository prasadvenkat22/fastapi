"""Run exactly one trading cycle, then exit.

Entry point for the system cron job. The in-app scheduler
(trading_engine/scheduler.py) is an asyncio task living inside the FastAPI
process, which means it dies with any container restart or redeploy and says
nothing about it — trading just quietly stops. Cron survives both, plus
droplet reboots, and its state is a crontab you can read rather than a module
global you have to interrogate over HTTP.

The guards that the scheduler loop used to own live here instead, in Python
rather than in a crontab expression, because market hours are defined in
America/New_York and shift with daylight saving. A UTC cron window would be
correct for half the year.

Exit codes: 0 ran or deliberately skipped, 1 the cycle failed.

IMPORTANT: never run this alongside the in-app scheduler. Both would open
positions against the same single open-position row.
"""

import asyncio
import logging
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("run_cycle")

NY = ZoneInfo("America/New_York")
MARKET_OPEN = (9, 30)
MARKET_CLOSE = (16, 0)


def _within_market_hours(now: datetime) -> bool:
    if now.weekday() >= 5:  # Saturday/Sunday
        return False
    return MARKET_OPEN <= (now.hour, now.minute) < MARKET_CLOSE


async def _run() -> int:
    from config.db_pgrs import SessionLocal
    from trading_engine.nodes import KILL_SWITCH_PATH
    from trading_engine.service import execute_and_persist_cycle

    now = datetime.now(NY)

    if os.path.exists(KILL_SWITCH_PATH):
        logger.info("skipped — kill switch active")
        return 0
    if not _within_market_hours(now):
        logger.info("skipped — outside market hours (%s ET)", now.strftime("%a %H:%M"))
        return 0

    db = SessionLocal()
    try:
        state = await execute_and_persist_cycle(db)
        logger.info(
            "cycle ok — action=%s playbook=%s exit=%s macd=%s trend=%s adx=%s/%s bb=%s rsi=%s macro=%s",
            state.get("execution_status"), state.get("playbook") or "-",
            state.get("exit_reason") or "-", state.get("macd_signal"),
            state.get("sma_trend"), state.get("adx"), state.get("adx_zone"),
            state.get("bollinger_zone"),
            state.get("rsi_zone"), state.get("market_sentiment"),
        )
        return 0
    except Exception:
        # Logged, not raised: cron mails on non-zero exit, and one bad cycle
        # (a data feed hiccup) should not become a stream of alerts. The next
        # minute tries again.
        logger.exception("cycle failed")
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_run()))
