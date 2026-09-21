"""Does the underlying's side of VWAP belong in the 0DTE STOP?  Section 208.

    python scripts/sweep_orphan_vwap_exit.py /tmp/engine_all.log [--exclude 2026-09-21]

Asked 2026-09-21 after the -10%/2min stop went live (section 207): "is VWAP
being watched before selling". It is not -- VWAP gates ENTRIES (vwap_gate,
until 10:30) and steps the resting ASK; the stop reads the mark, intrinsic and
the clock. This replays the same 130 structure-days as section 207 with the
underlying's position against its running session VWAP joined in from
Tradier's 5-minute bars (the same bars and the same construction vwap_gate
uses live), and asks whether a VWAP condition on the stop helps.

THE RULES, all at -10% with a 2-minute confirmation unless stated:

    intrinsic        production: fire only while intrinsic < entry
    mark-only        no guard at all (the section 207 open question)
    intr+vwap        production AND the underlying on the WRONG side of VWAP
                     for the structure -- a put debit needs spot ABOVE VWAP,
                     a call debit spot BELOW. Both guards must release.
    vwap-only        the VWAP side REPLACES the intrinsic guard
    intr|vwap        EITHER guard released (the union), so the stop fires when
                     the thesis has failed on price OR on the tape

A bar is looked up by the minute of each logged mark; the session VWAP at
that bar is the cumulative volume-weighted mean of Tradier's per-bar vwap up
to and including it. No bar (feed failure, first minutes) is treated as
"condition satisfied": a guard that cannot read must not hold a stop open.

Same counterfactual as every orphan sweep: a rule that never fires is scored
as the hold, valued at intrinsic against the real close (sweep_giveback).
Exits are replayed, entries are not; fills are the logged mark.
"""
from __future__ import annotations

import os
import sys
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sweep_giveback import expiry_value, load, pnl  # noqa: E402

NY = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")

STOP = -10.0
CONFIRM = 2

_BARS: dict = {}


def session_bars(root: str, day) -> list:
    """[(bar_start_et, close, running_vwap)] for one underlying-day, or []."""
    key = (root, str(day))
    if key in _BARS:
        return _BARS[key]
    out = []
    try:
        from trading_engine.vwap_gate import bars_for
        bars = bars_for(root, str(day))
        cum_pv = cum_v = 0.0
        for b in bars:
            t = datetime.fromisoformat(b["time"]).replace(tzinfo=NY)
            cum_pv += b["vwap"] * b["volume"]
            cum_v += b["volume"]
            vwap = cum_pv / cum_v if cum_v > 0 else b["close"]
            out.append((t, b["close"], vwap))
    except Exception as exc:  # noqa: BLE001 - report and carry on with the others
        print(f"  ! {root} {day}: no bars ({exc})", file=sys.stderr)
    _BARS[key] = out
    return out


def side_of_vwap(root: str, ts_utc: datetime, day) -> "bool | None":
    """True when spot is above the running session VWAP at the bar covering ts,
    False when below, None when no bar covers it."""
    bars = session_bars(root, day)
    if not bars:
        return None
    t = ts_utc.replace(tzinfo=UTC).astimezone(NY)
    hit = None
    for start, close, vwap in bars:
        if start <= t:
            hit = (close, vwap)
        else:
            break
    if hit is None:
        return None
    return hit[0] > hit[1]


def replay(rows, key, rule: str):
    """(exit_value, idx) or (None, None) for one structure under one rule."""
    day, root, right, lo, hi, _e = key
    entry = rows[0]["entry"]
    bullish = right == "C"          # debit only: call = bullish, put = bearish
    run = 0
    for idx, r in enumerate(rows):
        ret = (r["value"] - entry) / entry * 100.0
        breached = ret <= STOP
        intr_ok = r["intr"] < entry                 # thesis failed on price
        above = side_of_vwap(root, r["ts"], day)
        if above is None:
            vwap_ok = True                          # cannot read: do not hold a stop
        else:
            vwap_ok = (not above) if bullish else above   # wrong side for the structure
        if rule == "intrinsic":
            ok = breached and intr_ok
        elif rule == "mark-only":
            ok = breached
        elif rule == "intr+vwap":
            ok = breached and intr_ok and vwap_ok
        elif rule == "vwap-only":
            ok = breached and vwap_ok
        elif rule == "intr|vwap":
            ok = breached and (intr_ok or vwap_ok)
        else:
            raise ValueError(rule)
        run = run + 1 if ok else 0
        if ok and run > CONFIRM:
            return r["value"], idx
    return None, None


def main():
    path = sys.argv[1]
    exclude = set()
    if "--exclude" in sys.argv:
        exclude.add(sys.argv[sys.argv.index("--exclude") + 1])

    series = {k: v for k, v in load(path).items() if len(v) >= 10}
    keep = {}
    for k, rows in series.items():
        day, root, right, lo, hi, _e = k
        if rows[0]["credit"] or str(day) in exclude:
            continue
        ev = expiry_value(root, day, right, lo, hi, False)
        if ev is None:
            continue
        keep[k] = (rows, ev)
    print(f"{len(keep)} debit structure-days"
          + (f", excluding {sorted(exclude)}" if exclude else ""))

    # Coverage: how many logged minutes had a bar to read.
    have = miss = 0
    for k, (rows, _ev) in keep.items():
        for r in rows:
            if side_of_vwap(k[1], r["ts"], k[0]) is None:
                miss += 1
            else:
                have += 1
    print(f"VWAP read on {have} of {have + miss} logged minutes "
          f"({100.0 * have / max(1, have + miss):.0f}%)\n")

    hold_total = sum(pnl(rows[-1]["entry"], ev, rows[-1]["qty"], False)
                     for rows, ev in keep.values())
    print("%-11s %6s %6s %5s %10s %10s %10s %10s" % (
        "rule", "fires", "helps", "hurts", "rule $", "vs hold", "worst pos", "worst day"))
    per_day = {}
    for rule in ("intrinsic", "mark-only", "intr+vwap", "vwap-only", "intr|vwap"):
        fires = helps = hurts = 0
        total = 0.0
        worst = 0.0
        byday = defaultdict(float)
        for k, (rows, ev) in keep.items():
            last = rows[-1]
            hold = pnl(last["entry"], ev, last["qty"], False)
            val, idx = replay(rows, k, rule)
            if val is None:
                got = hold
            else:
                fires += 1
                got = pnl(rows[idx]["entry"], val, rows[idx]["qty"], False)
                helps += got > hold
                hurts += got < hold
            total += got
            worst = min(worst, got)
            byday[str(k[0])] += got
        per_day[rule] = byday
        print("%-11s %6d %6d %5d %+10.0f %+10.0f %+10.0f %+10.0f" % (
            rule, fires, helps, hurts, total, total - hold_total, worst, min(byday.values())))
    print("%-11s %6s %6s %5s %+10.0f" % ("hold", "-", "-", "-", hold_total))

    days = sorted({d for t in per_day.values() for d in t})
    print("\nper session  " + " ".join(f"{d[5:]:>7s}" for d in days))
    for rule, t in per_day.items():
        print(f"{rule:<12s} " + " ".join(f"{t.get(d, 0.0):+7.0f}" for d in days))


if __name__ == "__main__":
    main()
