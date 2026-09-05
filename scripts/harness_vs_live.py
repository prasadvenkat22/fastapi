"""Compare the harness's verdict with the live record, day by day.

The harness says -19.94/day over 60 sessions. The live engine booked +600 over
14 trades. Both describe the same strategy, so one of them is measuring
something other than what it claims.
"""
import os
import sys
os.environ.setdefault("SWEEP_CHAIN_PRICING", "1")
os.environ.setdefault("SWEEP_INTRABAR_STOPS", "true")
HERE = os.path.dirname(os.path.abspath(__file__))
for line in open(os.path.join(HERE, "trading.env"), encoding="utf-8"):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ[k] = v.strip().strip('"')
sys.path.insert(0, "C:/fastapi")

import scripts.sweep as S
import trading_engine.playbook as PB

# _patch_engine() installs the mock broker and the chain pricing. Without it
# _run_arm runs against an unpatched engine and takes ZERO trades -- which the
# first version of this script did, reporting +0.00 across 60 sessions as
# though that were a finding.
S._patch_engine()
sessions = S._load_sessions("60d")
PB.ENABLED_WINDOWS = frozenset({"MORNING_DRIFT", "AFTERNOON_CREDIT"})
trades, per_day = S._run_arm(sessions, S.dtime(9, 45))
by = {d["day"]: d for d in per_day}

# the live record, from TradeHistory
LIVE = {
    "2026-08-19": -19.34 - 25.28 + 111.95,
    "2026-08-20": +62.43,
    "2026-08-21": +56.27 + 15.12,
    "2026-08-24": 0.00,
    "2026-08-25": -23.00,
    "2026-08-27": -142.00 + 3.00 + 0.00,
    "2026-08-28": -106.00,
    "2026-09-02": +189.00,
    "2026-09-03": +478.00,
}
print()
print("  %-12s %10s %8s %10s %8s %10s" % (
    "session", "harness", "tr", "live", "tr", "difference"))
th = tl = 0.0
for d in sorted(LIVE):
    key = [k for k in by if str(k) == d]
    h = by[key[0]]["pnl"] if key else None
    hn = by[key[0]]["trades"] if key else 0
    lv = LIVE[d]
    th += (h or 0.0)
    tl += lv
    print("  %-12s %10s %8s %+10.2f %8s %10s" % (
        d, ("%+.2f" % h) if h is not None else "no session", hn, lv, "",
        ("%+.2f" % ((h or 0) - lv)) if h is not None else ""))
print("  %-12s %+10.2f %8s %+10.2f" % ("TOTAL", th, "", tl))
print()
print("  harness over ALL %d sessions: %+.2f (%.2f/day)" % (
    len(per_day), sum(d["pnl"] for d in per_day),
    sum(d["pnl"] for d in per_day) / max(len(per_day), 1)))
