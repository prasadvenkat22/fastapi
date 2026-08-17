"""Session-scoped market-breadth trend.

VIX gets its intraday move for free — the 1-minute bar series we already
fetch carries the whole session, so `fetch_vix()` can diff the latest print
against the open without storing anything. Breadth has no such luxury:
Tradier's quotes endpoint answers with a bare snapshot of who is up and who
is down *right now*, with no history attached. A single snapshot cannot tell
a market where breadth has been steadily strong from one that peaked at
+45 and has since collapsed to +3 — both read as "positive".

So each cycle's reading is written down and compared against the rest of
today's. The gate that matters is drawdown from a *recent* peak, not from
the session peak: breadth sliding from +48 at 10am to +20 by 2pm on a quiet
grind is ordinary drift, and anchoring on the session high would read that
as an emergency and then stay latched for the rest of the day. A rolling
window measures how fast participation is draining right now, which is the
thing worth reacting to, and it recovers on its own once the tape settles.

Session-anchored figures are still computed — they go into the sentiment
prompt so the model can weigh a slow all-day bleed qualitatively, which no
single hard threshold handles well.

Readings are normalised to a net ratio in [-1, 1] (addq / basket_size) so
thresholds survive any future resize of NASDAQ_BREADTH_BASKET.
"""

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from config.db_pgrs import SessionLocal
from models_pgdb.trading_models import BreadthReading

from .data_feed import MarketBreadth

logger = logging.getLogger(__name__)

NY = ZoneInfo("America/New_York")

# How far back the "is breadth draining right now" window reaches. At the
# scheduler's 5-minute cadence this is ~6 readings; it is time-based rather
# than count-based so a manually triggered cycle can't shrink the window.
RECENT_WINDOW_MINUTES = float(os.getenv("TRADING_BREADTH_WINDOW_MINUTES", "30"))


@dataclass
class BreadthTrend:
    net_ratio: float                   # current reading, normalised to [-1, 1]
    session_open_ratio: float          # first reading of today's session
    session_peak_ratio: float          # best reading of today's session
    recent_peak_ratio: float           # best reading within the recent window
    change_from_open: float            # current - open
    drawdown_from_session_peak: float  # current - session peak; always <= 0
    drawdown_from_recent_peak: float   # current - recent peak; always <= 0
    reading_count: int                 # readings recorded today, including this one

    @property
    def has_history(self) -> bool:
        """False on the session's first cycle, when there is nothing to
        compare against and every delta is trivially zero."""
        return self.reading_count > 1


def _session_start() -> datetime:
    """Midnight tonight, New York. The engine only trades the regular
    session, so a NY calendar day and a trading session cover the same
    readings — and anchoring on the calendar day keeps this correct across
    DST without tracking the exchange calendar."""
    return datetime.now(NY).replace(hour=0, minute=0, second=0, microsecond=0)


def record_and_summarize(breadth: MarketBreadth) -> BreadthTrend:
    """Persist this cycle's breadth reading and summarise today's trend.

    Best-effort: if the write or read fails, the caller still gets a valid
    no-history trend so the cycle can proceed on the point-in-time breadth
    check alone, rather than a database hiccup halting trading outright.
    """
    net_ratio = breadth.addq / breadth.basket_size if breadth.basket_size else 0.0

    try:
        db = SessionLocal()
        try:
            db.add(BreadthReading(
                addq=breadth.addq,
                advancers=breadth.advancers,
                decliners=breadth.decliners,
                unchanged=breadth.unchanged,
                basket_size=breadth.basket_size,
                net_ratio=net_ratio,
            ))
            db.commit()

            # Queried after the insert, so today's readings include this one
            # and the summary below is always self-consistent.
            rows = (
                db.query(BreadthReading.net_ratio, BreadthReading.recorded_at)
                .filter(BreadthReading.recorded_at >= _session_start())
                .order_by(BreadthReading.recorded_at)
                .all()
            )
            readings = [(r[0], r[1]) for r in rows]
        finally:
            db.close()
    except Exception:
        logger.exception("Breadth history unavailable — falling back to a point-in-time breadth read.")
        readings = []

    if not readings:
        readings = [(net_ratio, datetime.now(NY))]

    ratios = [r for r, _ in readings]
    window_start = readings[-1][1] - timedelta(minutes=RECENT_WINDOW_MINUTES)
    recent_ratios = [r for r, ts in readings if ts >= window_start] or [net_ratio]

    session_open_ratio = ratios[0]
    session_peak_ratio = max(ratios)
    recent_peak_ratio = max(recent_ratios)

    return BreadthTrend(
        net_ratio=net_ratio,
        session_open_ratio=session_open_ratio,
        session_peak_ratio=session_peak_ratio,
        recent_peak_ratio=recent_peak_ratio,
        change_from_open=net_ratio - session_open_ratio,
        drawdown_from_session_peak=net_ratio - session_peak_ratio,
        drawdown_from_recent_peak=net_ratio - recent_peak_ratio,
        reading_count=len(ratios),
    )
