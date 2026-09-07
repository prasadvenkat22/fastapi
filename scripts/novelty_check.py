"""Does dropping re-reports break the standing bearish tilt in the QQQ read?

THE CLAIM BEING TESTED. A market prices a story when it breaks. The wires then
re-report it every morning -- Iran, the Fed's next move, yields testing a level
-- and those re-reports land inside the 09:30 window looking like fresh news.
If that is what produced the QQQ read's standing bearish tilt (BEARISH on 10 of
14 sessions, 5/10 on direction, unmoved while the tape reversed, section 125),
then removing near-duplicates should move those verdicts and not much else.

If the tilt SURVIVES the filter, the cause is elsewhere -- the MACRO_TERMS set
itself is doom-weighted -- and this fix is the wrong one. Either answer is
worth having, which is why this compares rather than just applying.

    python scripts/novelty_check.py --symbols QQQ --show-scores
    python scripts/novelty_check.py --symbols QQQ,SNDK,NVDA --regrade
"""

from __future__ import annotations

import argparse
import os
import sys

import psycopg2

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trading_engine import symbol_news as SN


def _dsn() -> str:
    url = os.getenv("DATABASE_URL", "")
    return url.replace("postgresql+psycopg2://", "postgresql://").replace(
        "postgresql+asyncpg://", "postgresql://")


def graded_days(cur, sym):
    cur.execute("SELECT trading_day, verdict, confidence, headline_count "
                "FROM news_verdicts WHERE symbol=%s ORDER BY trading_day", (sym,))
    return cur.fetchall()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="QQQ")
    ap.add_argument("--show-scores", action="store_true",
                    help="print every headline with its similarity to prior coverage")
    ap.add_argument("--regrade", action="store_true",
                    help="call the model again on the filtered headlines and "
                         "compare the verdict to the one already stored")
    args = ap.parse_args()
    syms = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]

    conn = psycopg2.connect(_dsn())
    conn.autocommit = True
    cur = conn.cursor()

    print(f"novelty threshold {SN.NOVELTY_THRESHOLD:.2f}, "
          f"lookback {SN.NOVELTY_LOOKBACK_DAYS} days\n")

    for sym in syms:
        days = graded_days(cur, sym)
        if not days:
            print(f"{sym}: no graded days")
            continue
        print("=" * 86)
        print(f"{sym} — headlines kept after dropping re-reports")
        print("=" * 86)
        print(f"{'day':12s} {'all':>4s} {'new':>4s} {'dropped':>8s}  "
              f"{'stored verdict':16s} {'regraded on new only':20s}")

        tot_all = tot_new = 0
        moved = same = 0
        for day, v0, c0, n0 in days:
            allh = SN.session_headlines(sym, day, novel_only=False)
            scored = SN.session_headlines(sym, day, novel_only=True, with_scores=True)
            new = [h for h, _ in scored]
            tot_all += len(allh)
            tot_new += len(new)

            regraded = ""
            if args.regrade:
                if not new:
                    regraded = "NEUTRAL (nothing new)"
                else:
                    g = SN.classify_day(sym, day)
                    regraded = f"{g['verdict']} {g['confidence']:.2f}"
                    if g["verdict"] != v0:
                        moved += 1
                    else:
                        same += 1

            print(f"{str(day):12s} {len(allh):4d} {len(new):4d} "
                  f"{len(allh)-len(new):8d}  {v0:16s} {regraded:20s}")

            if args.show_scores:
                keep = {h for h, _ in scored}
                allscored = SN.session_headlines(sym, day, novel_only=False)
                simmap = {h: s for h, s in scored}
                for h in allscored:
                    mark = "NEW " if h in keep else "seen"
                    sim = simmap.get(h)
                    simtxt = f"{sim:.3f}" if sim is not None else "  -  "
                    print(f"    [{mark}] {simtxt}  {h[:88]}")

        print(f"\n  headlines: {tot_all} in window, {tot_new} new "
              f"({(tot_all-tot_new)/tot_all*100 if tot_all else 0:.0f}% were re-reports)")
        if args.regrade:
            print(f"  verdicts: {moved} changed, {same} unchanged")
            cur.execute("SELECT verdict, count(*) FROM news_verdicts "
                        "WHERE symbol=%s GROUP BY 1 ORDER BY 2 DESC", (sym,))
            print("  stored distribution: " + "  ".join(
                f"{v} {n}" for v, n in cur.fetchall()))
        print("")

    print("A filter that changes nothing has found nothing. If the verdict "
          "distribution is unmoved, the tilt is in the TERM SET, not in "
          "repetition, and MACRO_TERMS is where to look next.")
    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
