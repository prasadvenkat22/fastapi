"""Where is open interest being BUILT? Positioning, not urgency.

    python scripts/oi_flow.py --symbols SNDK,NVDA
    python scripts/oi_flow.py --symbols SNDK --min-change 200

WHY THIS IS DIFFERENT FROM flow.py. VWAP and signed volume infer who was
urgent -- they read the tape and guess at intent. Open interest is not an
inference: it counts contracts that exist. A strike whose OI rises by 4,000
overnight had 4,000 contracts OPENED there, and no amount of two-sided trading
produces that without someone taking a position and holding it.

WHAT IT STILL CANNOT TELL YOU. Whether the opener was long or short. A jump in
call OI at a strike above spot is consistent with a fund buying upside AND with
a dealer or an overwriter selling it. Volume against OI narrows it a little --
volume far above the OI change means most of the day's trading closed again --
but nothing here identifies the counterparty. It is a better class of evidence
than the tape, not proof.

    Read the OI change beside the DAY'S VOLUME at the same strike, and beside
    where spot sits. Large OI build at strikes just above spot in a near expiry
    is the interesting case; OI drift in a far-dated wing is noise.

OPEN INTEREST IS PUBLISHED ONCE A DAY, overnight, so every snapshot taken
during a session carries the same figure. This compares one snapshot PER
DATE; an intraday comparison would report zero change on every strike and
read as a quiet tape.

THE SERIES STARTS 2026-09-08. capture_chain.py collected 42 snapshots before
that carrying only [strike, c|p, bid, ask, iv, delta] -- no open interest at
all, and no historical option-chain feed exists to backfill it. So this reports
nothing until two sessions of the extended schema exist, which is a real wait
and not a bug.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SNAP_PATH = os.getenv("CHAIN_SNAPSHOT_PATH",
                      os.path.join(REPO_ROOT, "data", "qqq-chain-snapshots.jsonl"))


def load(path: str) -> list:
    """Snapshots that carry open interest, oldest first.

    Rows of length 6 predate 2026-09-08 and are skipped rather than defaulted
    to zero -- a missing measurement read as an OI of zero would show every
    strike gaining its entire open interest on the first extended snapshot.
    """
    out = []
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                snap = json.loads(line)
            except Exception:
                continue
            syms = snap.get("symbols")
            if not syms:
                continue
            keep = {}
            for entry in syms:
                sym = (entry.get("symbol") or "").upper()
                per = {}
                for exp in entry.get("expiries") or []:
                    rows = [r for r in (exp.get("rows") or []) if len(r) >= 8]
                    if rows:
                        per[exp.get("exp")] = rows
                if per:
                    keep[sym] = dict(spot=entry.get("spot"), expiries=per)
            if keep:
                out.append(dict(ts=snap.get("ts") or snap.get("captured_at") or "",
                                symbols=keep))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="")
    ap.add_argument("--min-change", type=int, default=100,
                    help="ignore strikes whose OI moved less than this")
    ap.add_argument("--top", type=int, default=12)
    ap.add_argument("--path", default=SNAP_PATH)
    args = ap.parse_args()
    want = {s.strip().upper() for s in args.symbols.split(",") if s.strip()}

    snaps = load(args.path)
    # OPEN INTEREST IS AN END-OF-DAY FIGURE. The exchange publishes it once,
    # overnight, so every snapshot taken during a session carries the SAME
    # OI and comparing 10:00 against 15:30 shows nothing. The comparison has
    # to be day over day, so keep one snapshot per date -- the last one,
    # which is the most likely to have a complete chain.
    by_day = {}
    for sn in snaps:
        by_day[str(sn["ts"])[:10]] = sn
    days = sorted(by_day)
    print(f"{len(snaps)} snapshot(s) carry open interest over "
          f"{len(days)} session(s), from {args.path}")
    if len(days) < 2:
        print("\nNEED TWO SESSIONS. Open interest is published once a day, and "
              "the extended schema began 2026-09-08 -- the 42 snapshots before "
              "it have no OI field and cannot be backfilled. Come back after "
              "the next session's capture_chain run.")
        return

    first, last = by_day[days[-2]], by_day[days[-1]]
    print(f"comparing {days[-2]}  ->  {days[-1]}\n")

    for sym in sorted(set(last["symbols"]) & set(first["symbols"])):
        if want and sym not in want:
            continue
        spot = last["symbols"][sym].get("spot")
        a = first["symbols"][sym]["expiries"]
        b = last["symbols"][sym]["expiries"]
        deltas = []
        for exp, rows in b.items():
            prior = {(r[0], r[1]): r for r in a.get(exp, [])}
            for r in rows:
                p = prior.get((r[0], r[1]))
                if not p:
                    continue
                change = r[6] - p[6]
                if abs(change) < args.min_change:
                    continue
                deltas.append((abs(change), exp, r[0], r[1], p[6], r[6],
                               change, r[7]))
        if not deltas:
            print(f"{sym}: no strike moved more than {args.min_change} contracts")
            continue
        deltas.sort(reverse=True)
        print(f"{sym}  spot {spot}")
        print(f"  {'expiry':12s} {'strike':>9s} {'cp':>3s} {'OI was':>8s} "
              f"{'OI now':>8s} {'change':>8s} {'vol':>8s} {'vs spot':>8s}")
        for _, exp, k, cp, was, now, ch, vol in deltas[:args.top]:
            rel = ((k / spot - 1) * 100) if spot else float("nan")
            print(f"  {exp:12s} {k:9.2f} {cp:>3s} {was:8d} {now:8d} "
                  f"{ch:+8d} {vol:8d} {rel:+7.1f}%")
        print("")

    print("OI UP AND VOLUME SIMILAR = positions opened and held. VOLUME MUCH "
          "LARGER THAN THE OI CHANGE = most of the day's trading closed again, "
          "so the build is smaller than the activity suggests.")
    print("Direction is still unknown: call OI rising above spot fits a fund "
          "buying upside AND a dealer selling it. This narrows the question, "
          "it does not answer it.")


if __name__ == "__main__":
    main()
