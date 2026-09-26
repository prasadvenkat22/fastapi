"""The trading schedule as the droplet's crontab has it, for the desk UI. Section 240.

The rotation's entry runs (single-stock 0DTE and the weekly book) live in the
host crontab, which the API container cannot read. A host cron line copies
`crontab -l` into the repo every ten minutes (CRONTAB_SNAPSHOT, gitignored);
this parses the dte0_trade.py lines out of it. Read-only: nothing here changes
the schedule, and a missing or stale snapshot is reported rather than guessed.

Cron hours are UTC on the droplet, so run times are converted to New York time
for the date asked about -- which also shows the hour moving when DST ends.
"""

from __future__ import annotations

import os
import shlex
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CRONTAB_SNAPSHOT = os.getenv("TRADING_CRONTAB_SNAPSHOT", os.path.join(_REPO, "crontab.snapshot"))
_DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]


def _field(spec: str, lo: int, hi: int) -> list[int]:
    """Expand one cron field: *, N, a-b, a,b and */n or a-b/n."""
    out: set[int] = set()
    for part in spec.split(","):
        step = 1
        if "/" in part:
            part, s = part.split("/", 1)
            step = int(s)
        if part == "*":
            a, b = lo, hi
        elif "-" in part:
            a, b = (int(x) for x in part.split("-", 1))
        else:
            a = b = int(part)
        out.update(range(a, b + 1, step))
    return sorted(x for x in out if lo <= x <= hi)


def _arg(argv: list[str], name: str, default: Optional[str] = None) -> Optional[str]:
    return argv[argv.index(name) + 1] if name in argv and argv.index(name) + 1 < len(argv) else default


def _expiry_label(book: str, expiry: Optional[str]) -> str:
    if book != "weekly":
        return "same day"
    if not expiry or expiry == "friday":
        return "this Friday Mon-Wed, next Friday after"
    if expiry.startswith("+"):
        return f"first Friday at least {expiry[1:]} days out"
    return expiry


def _label(book: str, expiry: Optional[str], on: date) -> str:
    """Section 245: a weekly run is 3-day or 7-day by how far out its expiry rule reaches."""
    if book != "weekly":
        return "Single-stock 0DTE"
    long_min = int(os.getenv("TRADING_WEEKLY_LONG_MIN_DAYS", "5") or 5)
    if expiry and expiry.startswith("+"):
        return "Single-stock 7-day" if int(expiry[1:]) >= long_min else "Single-stock 3-day"
    return "Single-stock 3-day"          # "friday": this Friday, Mon-Wed


def parse(crontab: str, on: Optional[date] = None) -> list[dict]:
    """The dte0_trade.py entry runs in a crontab, with run times in ET."""
    on = on or datetime.now(NY).date()
    jobs = []
    for line in crontab.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "dte0_trade.py" not in line:
            continue
        f = line.split(None, 5)
        if len(f) < 6:
            continue
        try:
            minutes, hours = _field(f[0], 0, 59), _field(f[1], 0, 23)
            dows = sorted({d % 7 for d in _field(f[4], 0, 7)})
            argv = shlex.split(f[5].split("dte0_trade.py", 1)[1].split(">>", 1)[0])
        except (ValueError, IndexError):
            continue
        book = _arg(argv, "--book", "dte0")
        times = sorted(
            datetime(on.year, on.month, on.day, h, m, tzinfo=timezone.utc).astimezone(NY).strftime("%H:%M")
            for h in hours for m in minutes)
        symbols = _arg(argv, "--symbols")
        jobs.append({
            "book": book,
            "label": _label(book, _arg(argv, "--expiry"), on),
            "days": [_DAYS[d] for d in dows],
            "times_et": times,
            "every_minutes": (int(f[0].split("/", 1)[1]) if f[0].startswith("*/") else None),
            "expiry": _expiry_label(book, _arg(argv, "--expiry")),
            "max_trades": int(_arg(argv, "--max-trades", "0") or 0) or None,
            "symbols": symbols.split(",") if symbols else None,
            "live_flag": "--live" in argv,
            "cron": " ".join(f[:5]) + " (UTC)",
        })
    return jobs


def snapshot(on: Optional[date] = None) -> dict:
    """The parsed schedule plus how fresh the crontab copy is."""
    try:
        with open(CRONTAB_SNAPSHOT, encoding="utf-8") as fh:
            text = fh.read()
        mtime = datetime.fromtimestamp(os.path.getmtime(CRONTAB_SNAPSHOT), timezone.utc)
    except FileNotFoundError:
        return {"jobs": [], "snapshot_at": None,
                "note": "no crontab snapshot yet -- the host cron line that writes it is not installed"}
    age = datetime.now(timezone.utc) - mtime
    return {"jobs": parse(text, on), "snapshot_at": mtime.isoformat(),
            "note": ("snapshot is over an hour old -- the copy job may have stopped"
                     if age > timedelta(hours=1) else None)}
