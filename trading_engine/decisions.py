"""Decision log: why the engine did or did not trade, one JSON line per event.

Section 273. The reasons already exist as log lines in /var/log/qqq-trading.log
on the host, which the API container cannot read. This module keeps the
interesting ones in decisions.jsonl beside trading_overrides.log (bind-mounted
at /app), so /trading/decisions can serve them to the desk.

Stdlib only, so the same parser backfills from the host's log:
    python3 trading_engine/decisions.py backfill /var/log/qqq-trading.log decisions.jsonl

Nothing here changes a trading decision: run_cycle attaches a logging handler
that copies matching records; a failure inside it is swallowed.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone

PATH = os.getenv("TRADING_DECISIONS_PATH", os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "decisions.jsonl"))

# (kind, regex on the message). First match wins. Order matters.
RULES: list[tuple[str, "re.Pattern[str]"]] = [(k, re.compile(p)) for k, p in (
    ("touch", r"^Band touch read: .*— (?!inside the band)"),
    ("refused", r"^Band touch: "),
    ("floor", r"^ACCOUNT FLOOR BREACHED"),
    ("sizing", r"^(Sizing:|Buying power:|Risk allocation:|Tail cap:)"),
    ("entry", r"^Entering "),
    ("entry_failed", r"^(ENTRY REJECTED|Open refused|QQQ 0DTE bucket is OFF)"),
    ("exit", r"^(Engine take-profit|Engine profit lock|Band touch: QQQ .* booking)"),
    ("stop_wait", r"^QQQ is [-+0-9.]+%, past the"),
    ("exit", r"^ORPHAN [A-Z_]+: closing "),
    ("booked", r"^ORPHAN booked to history"),
    ("exit_failed", r"^ORPHAN .*(did not fill|wants .* but an order)"),
    ("halt", r"(consecutive losing trades today|daily loss|HALTED)"),
    ("macro", r"^(Yields spiking|Macro halt|VIX .*halt)"),
)]
# "Band touch: ..." lines that are really exits, not refusals.
_EXIT_BOOKING = re.compile(r"booking")
_LINE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ (\w+) (.*)$")
# The same reason repeated every minute is one story: keep one per 10 minutes.
_DEDUPE_MIN = {"floor": 30, "macro": 30, "stop_wait": 0, "refused": 0}
_last: dict = {}


def classify(msg: str) -> "str | None":
    if msg.startswith("Band touch read: ") and "inside the band" in msg:
        return None
    for kind, rx in RULES:
        if rx.search(msg):
            if kind == "refused" and _EXIT_BOOKING.search(msg):
                return "exit"
            return kind
    return None


def event_from_line(line: str) -> "dict | None":
    """A decision event from one log line (host timestamps are UTC), or None."""
    m = _LINE.match(line.rstrip("\n"))
    if not m:
        return None
    ts, level, msg = m.groups()
    kind = classify(msg)
    if kind is None:
        return None
    return {"ts": ts.replace(" ", "T") + "Z", "kind": kind, "level": level, "text": msg[:400]}


def _keep(ev: dict) -> bool:
    """Collapse repeats of the same event within its dedupe window."""
    window = _DEDUPE_MIN.get(ev["kind"], 0)
    if not window:
        return True
    key = (ev["kind"], re.sub(r"[-+]?\d+(\.\d+)?", "#", ev["text"]))
    t = datetime.fromisoformat(ev["ts"].rstrip("Z"))
    prev = _last.get(key)
    if prev is not None and t - prev < timedelta(minutes=window):
        return False
    _last[key] = t
    return True


def append(ev: dict, path: "str | None" = None) -> None:
    with open(path or PATH, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(ev) + "\n")


class DecisionHandler(logging.Handler):
    """Copies decision-bearing records into decisions.jsonl. Never raises."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
            kind = classify(msg)
            if kind is None:
                return
            ts = datetime.fromtimestamp(record.created, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            ev = {"ts": ts, "kind": kind, "level": record.levelname, "text": msg[:400]}
            if _keep(ev):
                append(ev)
        except Exception:
            pass


def read_day(day: str, path: "str | None" = None) -> list:
    """Events whose New York calendar day is `day` (YYYY-MM-DD), oldest first."""
    from zoneinfo import ZoneInfo
    ny = ZoneInfo("America/New_York")
    out = []
    try:
        with open(path or PATH, encoding="utf-8") as fh:
            for ln in fh:
                try:
                    ev = json.loads(ln)
                    t = datetime.fromisoformat(ev["ts"].rstrip("Z")).replace(tzinfo=timezone.utc)
                except (ValueError, KeyError):
                    continue
                if t.astimezone(ny).strftime("%Y-%m-%d") == day:
                    ev["et"] = t.astimezone(ny).strftime("%H:%M:%S")
                    out.append(ev)
    except FileNotFoundError:
        return []
    out.sort(key=lambda e: e["ts"])
    return out


def backfill(src: str, dst: str) -> int:
    """Rebuild dst from a plain cycle log (idempotent: dst is replaced)."""
    n = 0
    with open(src, encoding="utf-8", errors="replace") as fi, open(dst, "w", encoding="utf-8") as fo:
        for ln in fi:
            ev = event_from_line(ln)
            if ev and _keep(ev):
                fo.write(json.dumps(ev) + "\n")
                n += 1
    return n


if __name__ == "__main__" and len(sys.argv) == 4 and sys.argv[1] == "backfill":
    print(backfill(sys.argv[2], sys.argv[3]), "events")
