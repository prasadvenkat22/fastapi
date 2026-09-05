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

from trading_engine import market_calendar

NY = ZoneInfo("America/New_York")
MARKET_OPEN = (9, 30)
MARKET_CLOSE = (16, 0)

# How often to re-check exits WHILE A POSITION IS OPEN, in seconds.
#
# Cron fires this script once a minute, so every exit -- the stop included --
# waits up to 60 seconds for the next process. That is the dominant loss
# channel and it was mistaken for a footnote for two days. Measured over 60
# sessions on 5-minute bars, the 19 losing morning trades realised a MEAN of
# -28.3% against a stop set to -8%, and the worst realised -66.4% five minutes
# after entry. The live overshoot is smaller because the live interval is one
# minute rather than five -- 2026-08-28 realised -18.1% against a -10% stop,
# 1.8x -- but it is the largest single item measured on this engine.
#
# Polling only happens while a position is OPEN. Flat cycles run exactly once,
# as before, so entry logic, breadth and the LLM verdict are untouched in
# frequency. The macro verdict is cached on a 5-minute refresh, so even the
# polled cycles do not multiply the LLM cost.
#
# 0 disables it and restores the previous behaviour exactly.
EXIT_POLL_SECONDS = float(os.getenv("TRADING_EXIT_POLL_SECONDS", "0"))

# Never poll past this; cron will start the next process at the minute.
_POLL_BUDGET_SECONDS = 55.0


def _within_market_hours(now: datetime) -> bool:
    """Is the exchange actually open right now?

    This used to read the weekday and the clock alone, which is right four
    days in five and wrong on the ten or so sessions a year the market is
    shut or closes early. On those the engine ran every cycle against the
    PREVIOUS session's stale quotes, computed indicators from them, and was
    free to open a position. Found 2026-09-05, two days before Labor Day.
    """
    if not market_calendar.is_trading_day(now.date()):
        return False
    close = market_calendar.close_time_for(now.date())
    return MARKET_OPEN <= (now.hour, now.minute) < (close.hour, close.minute)


async def _poll_exits(db) -> None:
    """Re-run the cycle every EXIT_POLL_SECONDS while a position is open.

    Deliberately re-uses execute_and_persist_cycle rather than carving out an
    exit-only path. The exit rules live inside execution_risk_agent and need
    the full indicator state, so a separate lightweight path would be a second
    implementation of the exit ladder -- and a second implementation that
    drifts is worse than a slower one that does not.

    Stops the moment the position closes, so a polled minute costs nothing
    once the trade is out.
    """
    from models_pgdb.trading_models import OpenPosition
    from trading_engine.service import execute_and_persist_cycle

    if EXIT_POLL_SECONDS <= 0:
        return
    if db.query(OpenPosition).first() is None:
        return

    waited = 0.0
    while waited + EXIT_POLL_SECONDS <= _POLL_BUDGET_SECONDS:
        await asyncio.sleep(EXIT_POLL_SECONDS)
        waited += EXIT_POLL_SECONDS
        db.expire_all()
        if db.query(OpenPosition).first() is None:
            logger.info("exit poll: position closed after %.0fs — stopping.", waited)
            return
        try:
            state = await execute_and_persist_cycle(db)
        except Exception:
            logger.exception("exit poll failed at %.0fs — leaving it to the next cycle.", waited)
            return
        logger.info(
            "exit poll +%.0fs — action=%s exit=%s",
            waited, state.get("execution_status"), state.get("exit_reason") or "-",
        )


async def _run() -> int:
    from config.db_pgrs import SessionLocal
    from trading_engine.nodes import KILL_SWITCH_PATH
    from trading_engine.service import execute_and_persist_cycle

    now = datetime.now(NY)

    if os.path.exists(KILL_SWITCH_PATH):
        logger.info("skipped — kill switch active")
        return 0
    if not _within_market_hours(now):
        if now.weekday() >= 5:
            why = "weekend"
        elif not market_calendar.is_trading_day(now.date()):
            why = "market holiday"
        else:
            why = "outside market hours"
        logger.info("skipped — %s (%s ET)", why, now.strftime("%a %H:%M"))
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
        await _poll_exits(db)
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
