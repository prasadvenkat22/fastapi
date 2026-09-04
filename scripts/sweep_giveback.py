"""Sweep the orphan giveback rule against the engine's own logged series.

WHY NOT scripts/sweep.py: that harness replays the ENGINE's QQQ 0DTE trades.
It has never replayed orphans.py -- section 75 -- so it cannot answer a
question about an orphan exit rule at all. What it can be replaced by here is
better in one respect and worse in two.

BETTER: this is not a simulation. Every row is a real position the account
actually held, marked by the real chain, one observation a minute.

WORSE, and both matter when reading the numbers:

  CENSORING, WHICH THE FIRST VERSION OF THIS GOT WRONG AND HAD TO BE FIXED.
  Taking the last logged value as the "hold" outcome is CIRCULAR: firing the
  rule closes the position, which ends the log, so "hold" becomes the exit
  price itself. On MU 990/1000 that recorded holding as -390 -- the very exit
  under test -- and made the rule look profitable at every setting. The
  recovery to full intrinsic an hour later was invisible because the series
  stopped at the exit.

  The counterfactual is now the TRUE value at expiry, computed from the
  underlying's actual close that day, so holding is valued at what it really
  paid rather than at where the rule chose to stop watching.

  SMALL AND ONE-SIDED. A handful of sessions, dominated by two names on days
  they happened to move. Nothing here is a distribution.

So this measures whether the rule would have helped ON THE DAYS OBSERVED,
which is the question actually being asked, and is not evidence about days
that were not observed.
"""
import os
import re
import sys
from collections import defaultdict
from datetime import datetime

import yfinance as yf

_CLOSES = {}


def close_on(root, day):
    """The underlying's actual close on `day`, for valuing the hold at expiry."""
    key = (root, day)
    if key in _CLOSES:
        return _CLOSES[key]
    try:
        h = yf.Ticker(root).history(start=str(day), end=str(day + __import__("datetime").timedelta(days=4)))
        px = float(h["Close"].iloc[0]) if len(h) else None
    except Exception:
        px = None
    _CLOSES[key] = px
    return px


def expiry_value(root, day, right, lo, hi, credit):
    """What the structure was worth at expiry: intrinsic against the real close."""
    px = close_on(root, day)
    if px is None:
        return None
    lo, hi = float(lo), float(hi)
    a, b = min(lo, hi), max(lo, hi)
    if right == "C":
        val = max(0.0, min(px - a, b - a))
    else:
        val = max(0.0, min(b - px, b - a))
    # For a credit structure lo/hi already resolve so that this is the cost to
    # close; for a debit it is the value received. Section 70.
    return val

LINE = re.compile(
    r"^(?P<ts>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+\s+INFO ORPHAN "
    r"(?P<root>[A-Z]+) (?P<right>[CP]) (?P<lo>[\d.]+)/(?P<hi>[\d.]+) "
    r"x(?P<qty>\d+) (?P<kind>debit|credit): entry (?P<entry>[\d.]+) "
    r"value (?P<value>[\d.]+) (?P<ret>[-+][\d.]+)%.*?"
    r"\[intrinsic (?P<intr>[-\d.]+), extrinsic (?P<extr>[-+\d.]+)\]"
)


def load(path):
    series = defaultdict(list)
    for raw in open(path, encoding="utf-8", errors="replace"):
        m = LINE.match(raw.strip())
        if not m:
            continue
        d = m.groupdict()
        ts = datetime.strptime(d["ts"], "%Y-%m-%d %H:%M:%S")
        key = (ts.date(), d["root"], d["right"], d["lo"], d["hi"], d["entry"])
        series[key].append({
            "ts": ts,
            "qty": int(d["qty"]),
            "credit": d["kind"] == "credit",
            "entry": float(d["entry"]),
            "value": float(d["value"]),
            "intr": float(d["intr"]),
            "width": abs(float(d["hi"]) - float(d["lo"])),
        })
    for k in series:
        series[k].sort(key=lambda r: r["ts"])
    return series


def replay(rows, pct, confirm_min):
    """Return (exit_value, exit_index) for the giveback rule, or (None, None)."""
    peak = None
    gb_since = None
    need = rows[0]["width"] * pct / 100.0
    credit = rows[0]["credit"]
    entry = rows[0]["entry"]
    for idx, r in enumerate(rows):
        i = r["intr"]
        if peak is None or (i < peak if credit else i > peak):
            peak = i
        was_winner = (peak < entry) if credit else (peak > entry)
        given = (i - peak) if credit else (peak - i)
        if was_winner and given >= need:
            if gb_since is None:
                gb_since = r["ts"]
            held = (r["ts"] - gb_since).total_seconds() / 60.0
            if held >= confirm_min:
                return r["value"], idx
        else:
            gb_since = None
    return None, None


def pnl(entry, value, qty, credit):
    return ((entry - value) if credit else (value - entry)) * qty * 100


def main():
    path = sys.argv[1]
    series = load(path)
    # Only structures with enough observations to be a series at all.
    series = {k: v for k, v in series.items() if len(v) >= 10}
    print(f"{len(series)} structure-days, "
          f"{sum(len(v) for v in series.values())} observations\n")

    print("  %-6s %-8s %6s %6s %10s %10s %10s" % (
        "give%", "confirm", "fires", "helps", "rule P&L", "hold P&L", "difference"))
    for pct in (10, 15, 20, 30, 40):
        for confirm in (0, 5, 10, 15):
            fired = helped = 0
            rule_total = hold_total = 0.0
            for (day, root, right, lo, hi, _e), rows in series.items():
                last = rows[-1]
                ev_exp = expiry_value(root, day, right, lo, hi, last["credit"])
                if ev_exp is None:
                    continue
                hold = pnl(last["entry"], ev_exp, last["qty"], last["credit"])
                ev, idx = replay(rows, pct, confirm)
                if ev is None:
                    rule = hold
                else:
                    fired += 1
                    rule = pnl(rows[idx]["entry"], ev, rows[idx]["qty"], rows[idx]["credit"])
                    if rule > hold:
                        helped += 1
                rule_total += rule
                hold_total += hold
            print("  %-6d %-8s %6d %6d %+10.0f %+10.0f %+10.0f" % (
                pct, f"{confirm:.0f} min", fired, helped,
                rule_total, hold_total, rule_total - hold_total))
        print()


if __name__ == "__main__":
    main()
