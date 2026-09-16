"""Replay a session's positions against different exit settings.

    python scripts/exit_backtest.py                 # today
    python scripts/exit_backtest.py 2026-09-15      # a past session
    python scripts/exit_backtest.py 2026-09-15 QQQ  # one underlying

WHAT IS REAL: the marks. Every row comes from the engine's own ORPHAN log
lines -- the value it actually saw, at the time it saw it, priced at what it
could have transacted at. No reconstruction, no model, no assumption about
what a spread was "worth".

WHAT IS NOT, and both matter when reading the output:

  TRUNCATION BIASES AGAINST PATIENT RULES. A rule that would have held LONGER
  than the real exit has no data past that point, so those runs stop at the
  last observed mark and are scored there. They show as HELD. On 2026-09-15,
  three of six QQQ positions truncated, so the patient configurations are
  understated by an unknown amount.

  IT REPLAYS EXITS, NOT ENTRIES. Nothing here can say whether a different tier
  or window would have been profitable, because a trade that was never taken
  left no marks. Changes to ITM_GRINDER, ZONE tiers or the news gates are
  invisible to this tool.

READS ROTATED LOGS TOO. logrotate compresses the engine log several times a
day on this box, and the first version of this script read only the live file
-- which on a quiet evening held about forty lines and none of the session.
A backtest that silently sees a tenth of the data is worse than no backtest.

WHAT IT IS FOR: arguing a setting change against a session's marks instead of
against the most recent trade. That distinction is not academic -- the stall
window was changed on one trade's evidence and this script reversed it the
same evening.
"""

from __future__ import annotations

import glob
import gzip
import re
import sys
from collections import defaultdict
from datetime import date, datetime

LOG_GLOB = "/var/log/qqq-trading.log*"

# entry/value/return as the engine logged them, per cycle, per position.
RX = re.compile(
    r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ INFO ORPHAN ([A-Z]+) [CP] "
    r"(\d+)/(\d+) x(\d+) (debit|credit): entry ([\d.]+) value ([\d.]+) "
    r"([-+][\d.]+)%"
)

# (label, target, stop, stop-confirm minutes, stall minutes, giveback %)
CONFIGS = [
    ("live before 2026-09-15  tgt30 stop-10 cf0 stall5/20", 30, -10, 0, 5, 20),
    ("LIVE NOW                tgt30 stop-35 cf5 stall5/20", 30, -35, 5, 5, 20),
    ("                        tgt30 stop-35 cf2 stall5/20", 30, -35, 2, 5, 20),
    ("                        tgt30 stop-35 cf5 stall2/20", 30, -35, 5, 2, 20),
    ("                        tgt30 stop-25 cf5 stall5/20", 30, -25, 5, 5, 20),
    ("                        tgt40 stop-35 cf5 stall5/20", 40, -35, 5, 5, 20),
    ("                        no exits, ride to last mark", 999, -999, 0, 999, 999),
]


def load(day: str, only: str = "") -> dict:
    """{(symbol, strikes, entry): [(t, value, ret, qty)]} for one session."""
    out = defaultdict(list)
    for fn in sorted(glob.glob(LOG_GLOB)):
        opener = gzip.open if fn.endswith(".gz") else open
        try:
            fh = opener(fn, "rt", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                m = RX.match(line)
                if not m:
                    continue
                ts, sym, lo, hi, qty, kind, entry, val, ret = m.groups()
                if not ts.startswith(day):
                    continue
                if only and sym != only.upper():
                    continue
                out[(sym, f"{lo}/{hi}", float(entry))].append(
                    (datetime.strptime(ts, "%Y-%m-%d %H:%M:%S"),
                     float(val), float(ret), int(qty)))
    # A cycle can log the same position more than once; dedupe and order.
    return {k: sorted(set(v)) for k, v in out.items()}


def run(marks, target, stop, confirm, stall_min, giveback):
    """Walk the marks once. First rule to fire wins, as the engine does."""
    peak = peak_at = stop_since = None
    for t, _val, ret, qty in marks:
        if peak is None or ret > peak:
            peak, peak_at = ret, t
        if ret >= target:
            return "TARGET", ret, qty
        if peak and peak > 0:
            quiet = (t - peak_at).total_seconds() / 60.0
            if quiet >= stall_min and ret <= peak * (1 - giveback / 100.0):
                return "STALL", ret, qty
        if ret <= stop:
            if stop_since is None:
                stop_since = t
            elif (t - stop_since).total_seconds() / 60.0 >= confirm:
                return "STOP", ret, qty
        else:
            # Recovery clears the clock, the way the live rule does.
            stop_since = None
    return "HELD", marks[-1][2], marks[-1][3]


def main() -> None:
    day = sys.argv[1] if len(sys.argv) > 1 else date.today().isoformat()
    only = sys.argv[2] if len(sys.argv) > 2 else ""
    series = load(day, only)
    if not series:
        print(f"No ORPHAN marks found for {day}"
              f"{' / ' + only.upper() if only else ''}.")
        print("The engine only logs these while a position is open.")
        return

    print(f"\n{day}{' — ' + only.upper() if only else ''}: "
          f"{len(series)} position(s) with logged marks\n")
    for (sym, strikes, entry), marks in sorted(series.items()):
        print(f"  {sym:<5} {strikes:<9} entry {entry:>6.2f}  {len(marks):>4} marks  "
              f"peak {max(m[2] for m in marks):+7.1f}%  "
              f"worst {min(m[2] for m in marks):+7.1f}%")

    print(f"\n{'config':<54}{'total $':>10}  outcomes")
    print("-" * 104)
    for name, tgt, stop, cf, sm, gb in CONFIGS:
        total, outs, truncated = 0.0, [], 0
        for (sym, _strikes, entry), marks in sorted(series.items()):
            why, ret, qty = run(marks, tgt, stop, cf, sm, gb)
            total += entry * (ret / 100.0) * qty * 100
            truncated += why == "HELD"
            outs.append(f"{why[0]}{ret:+.0f}")
        print(f"{name:<54}{total:>+10.0f}  {' '.join(outs)}"
              f"{'   (' + str(truncated) + ' held)' if truncated else ''}")

    print("\nT=target S=stall P=stop H=held to the last mark.")
    print("HELD means the rule would have run past the real exit and there is "
          "no data beyond it, so it is scored where the marks stop. That "
          "understates patient configurations.")
    print("EXITS ONLY. A tier or window change alters which trades are TAKEN, "
          "and a trade never taken leaves no marks — this cannot see it.")


if __name__ == "__main__":
    main()
