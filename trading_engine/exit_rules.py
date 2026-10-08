"""Exit rules shared by the QQQ engine (nodes.py) and the 0DTE ladder (orphans.py).

Section 271. Both are pure functions over a small dict the caller persists, so
the engine -- a fresh process every minute -- and the ladder keep the same
clocks across cycles.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

# A cycle runs every minute (the engine also polls every few seconds while a
# position is open). A gap longer than this between two readings past the stop
# is an outage, not time spent past the stop, and is not counted toward the
# total.
MAX_GAP_MINUTES = 2.0


def stop_confirm_total() -> bool:
    """TRADING_STOP_CONFIRM_TOTAL: count TOTAL minutes past the stop rather than
    minutes IN A ROW. Read per call so a settings save applies on the next cycle."""
    return os.getenv("TRADING_STOP_CONFIRM_TOTAL", "false").lower() == "true"


def stop_clock(rec: dict, breaching: bool, now: datetime, total: bool) -> float:
    """Minutes the position has spent past its stop, updating `rec` in place.

    In a row (total=False): the count starts at the first reading past the stop
    and goes back to zero the moment a reading recovers -- the rule both exit
    paths have always used.

    Total (total=True): every minute past the stop counts and a recovery only
    pauses the count. On 10-07 the QQQ 752/750 put sat past -10% for 9 of 10
    minutes, recovered for one reading, and started over -- twice.
    """
    if not total:
        rec.pop("stop_total", None)
        rec.pop("stop_last", None)
        if not breaching:
            rec.pop("stop_since", None)
            return 0.0
        since = datetime.fromisoformat(rec.setdefault("stop_since", now.isoformat()))
        return max(0.0, (now - since).total_seconds() / 60.0)

    rec.pop("stop_since", None)
    held = float(rec.get("stop_total") or 0.0)
    if not breaching:
        rec.pop("stop_last", None)
        return held
    last = rec.get("stop_last")
    if last:
        gap = (now - datetime.fromisoformat(last)).total_seconds() / 60.0
        held += min(max(gap, 0.0), MAX_GAP_MINUTES)
    rec["stop_total"] = held
    rec["stop_last"] = now.isoformat()
    return held


def clear_stop_clock(rec: dict) -> None:
    for k in ("stop_since", "stop_total", "stop_last"):
        rec.pop(k, None)


def profit_lock(peak_pct: float, ret_pct: float,
                arm_pct: Optional[float], floor_pct: Optional[float]) -> bool:
    """True when a trade that has been up `arm_pct` is back to `floor_pct`.

    Both are return on cost. None for either = off. The 10-07 put reached
    +36.6%, its +30% target did not fill at the mid, and nothing stopped it
    sliding to a loss.
    """
    if arm_pct is None or floor_pct is None:
        return False
    return peak_pct >= arm_pct and ret_pct <= floor_pct


def opt_float(name: str) -> Optional[float]:
    """An env setting where blank means off."""
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        logger.warning("%s=%r is not a number -- treated as off.", name, raw)
        return None


# --- Engine stop clock, persisted -------------------------------------------
# The engine kept this clock in a module dict, and run_cycle.py is a new process
# every minute (polling for at most 55 s), so any confirmation of a minute or
# more could never complete: from 10-06 11:51 ET (confirmation 10) an engine
# spread past its stop would have held to the 15:45 force close.
ENGINE_CLOCK_PATH = os.getenv("TRADING_ENGINE_STOP_CLOCK", "engine_stop_clock.json")


def load_engine_clock(key: str) -> dict:
    try:
        with open(ENGINE_CLOCK_PATH) as fh:
            d = json.load(fh)
    except (FileNotFoundError, ValueError):
        return {}
    if not isinstance(d, dict) or d.get("key") != key:
        return {}      # a different position: its clock does not carry over
    return d


def save_engine_clock(key: str, rec: dict) -> None:
    try:
        with open(ENGINE_CLOCK_PATH, "w") as fh:
            json.dump(dict(rec, key=key), fh)
    except Exception:
        logger.exception("Could not persist the engine stop clock -- the confirmation restarts.")
