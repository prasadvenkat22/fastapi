"""Per-symbol news watcher: grade today's news, decide a structure, review
what is already open.

Runs pre-open and hourly. The macro half of the news read already fires every
five minutes inside the trading cycle (MACRO_REFRESH_MINUTES); this is the
per-symbol half, which had no schedule at all -- classify_day() existed and
nothing called it.

WHY IT IS CHEAP TO RUN HOURLY. Same-day sentiment and hourly firing pull
against each other: re-running the classifier every hour would mostly restate
the 09:15 answer at 15 symbols x 7 hours = 105 Claude calls a day. So it fires
ON CHANGE and checks hourly -- a digest of the day's headline set is stored
with the verdict, and an unchanged digest skips the model. A quiet name costs
one SELECT; a name that just broke news is re-graded within the hour.

WHAT IT DOES AND DOES NOT DO. It writes a verdict, a suggested structure, and
an action for any open position in that name. IT DOES NOT TRADE. On the 365
labelled rows this repository now has, being in the news predicts nothing --
the means are inside +/-0.6 ATR with larger standard deviations, and 35% of
the sample is one trending name. Section 22 (crude) and section 14 (the macro
LLM verdict) are the precedent: a term that has never been measured is logged
beside the decision, not wired into it. When news_symbol_impact has enough
rows to say whether VERY_BULLISH actually precedes a move, that is the moment
to consider gating.

    python scripts/news_watch.py            # all tracked symbols
    python scripts/news_watch.py --symbols SNDK,MU
    python scripts/news_watch.py --force    # ignore the digest, re-grade
"""

import argparse
import hashlib
import os
import sys
import uuid
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import psycopg2

from trading_engine.symbol_news import ALIASES, classify_day, same_day_headlines

NY = ZoneInfo("America/New_York")

# Verdict -> what to put on if nothing is open. Debit spreads both ways: the
# 0DTE book's own grid says a long structure wants a shallow ITM long leg and
# real OTM room, and the same asymmetry applies whichever side you take.
STRUCTURE = {
    "VERY_BULLISH": "CALL_DEBIT_SPREAD",
    "BULLISH": "CALL_DEBIT_SPREAD",
    "NEUTRAL": "NO_NEW_TRADE",
    "BEARISH": "PUT_DEBIT_SPREAD",
    "VERY_BEARISH": "PUT_DEBIT_SPREAD",
}

BULLISH_POS = ("BULL_CALL_SPREAD", "PUT_CREDIT_SPREAD")
BEARISH_POS = ("BEAR_PUT_SPREAD", "CALL_CREDIT_SPREAD")


def _dsn() -> str:
    url = os.getenv("DATABASE_URL", "")
    return url.replace("postgresql+psycopg2://", "postgresql://").replace(
        "postgresql+asyncpg://", "postgresql://")


def digest(heads) -> str:
    return hashlib.sha256("".join(sorted(heads)).encode("utf-8")).hexdigest()[:32]


def position_action(strategy: str, verdict: str) -> str:
    """What to do about a position already open in this name.

    Only a VERY_* verdict against the position is called a conflict. An
    ordinary BEARISH read against a bull spread is noise at this sample size,
    and a rule that flags it would fire constantly and be ignored -- which is
    how a guard stops being read at all.
    """
    if strategy in BULLISH_POS:
        if verdict == "VERY_BEARISH":
            return "REVIEW_EXIT — very bearish news against a bullish position"
        if verdict in ("VERY_BULLISH", "BULLISH"):
            return "HOLD — news agrees with the position"
    if strategy in BEARISH_POS:
        if verdict == "VERY_BULLISH":
            return "REVIEW_EXIT — very bullish news against a bearish position"
        if verdict in ("VERY_BEARISH", "BEARISH"):
            return "HOLD — news agrees with the position"
    return "HOLD — news is neutral or unrelated to the position's direction"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    syms = ([s.strip().upper() for s in args.symbols.split(",") if s.strip()]
            or sorted(ALIASES))
    now = datetime.now(NY)

    # OWNS ITS OWN CLOCK, like run_cycle.py. The crontab is UTC, so a fixed
    # UTC schedule lands an hour off for the half of the year that is not
    # EDT -- the 09:15 pre-open run would fire at 08:15 in winter, before any
    # news exists. So cron is scheduled WIDE enough to cover both offsets and
    # this check decides whether the run is real. Weekends exit here too,
    # which is why a Saturday invocation prints nothing and costs nothing.
    if not args.force:
        # market_calendar, not weekday() -- the cron fires Mon-Fri and would
        # otherwise run on Labor Day, Thanksgiving and Good Friday. The
        # calendar already carries the observed-date shifts (Independence Day
        # 2026 is observed on the 3rd because the 4th is a Saturday), which a
        # weekday check cannot know.
        from trading_engine.market_calendar import is_trading_day

        if not is_trading_day(now.date()):
            print(f"{now:%Y-%m-%d %H:%M %Z} — not a trading day, nothing to do.")
            return
        if not (dtime(8, 45) <= now.time() <= dtime(16, 30)):
            print(f"{now:%Y-%m-%d %H:%M %Z} — outside 08:45-16:30 ET, nothing to do.")
            return

    day = now.date()
    conn = psycopg2.connect(_dsn())
    conn.autocommit = True
    cur = conn.cursor()

    # What is already open, so the verdict can be turned into an action.
    open_by_symbol = {}
    try:
        cur.execute("SELECT underlying, strategy FROM trading_open_positions")
        for u, s in cur.fetchall():
            open_by_symbol.setdefault((u or "").upper(), []).append(s)
    except Exception as exc:
        print(f"(open positions unreadable: {exc})")

    print(f"NEWS WATCH  {datetime.now(NY):%Y-%m-%d %H:%M %Z}  trading day {day}\n")
    print(f"{'sym':6s} {'verdict':14s} {'conf':>5s} {'n':>3s} {'structure':20s} action")
    for sym in syms:
        heads = same_day_headlines(sym, day)
        d = digest(heads) if heads else None

        cur.execute("SELECT headline_digest, verdict, confidence, rationale "
                    "FROM news_verdicts WHERE symbol=%s AND trading_day=%s", (sym, day))
        prev = cur.fetchone()

        if not heads:
            print(f"{sym:6s} {'(no news today)':14s}")
            continue
        if prev and prev[0] == d and not args.force:
            v, c = prev[1], prev[2]
            act = "; ".join(position_action(s, v) for s in open_by_symbol.get(sym, [])) or "-"
            print(f"{sym:6s} {v:14s} {c or 0:5.2f} {len(heads):3d} "
                  f"{STRUCTURE.get(v, '?'):20s} {act}   (unchanged, no model call)")
            continue

        res = classify_day(sym, day)
        v = res["verdict"]
        structure = STRUCTURE.get(v, "NO_NEW_TRADE")
        actions = [position_action(s, v) for s in open_by_symbol.get(sym, [])]
        act = "; ".join(actions) or "-"
        cur.execute(
            """
            INSERT INTO news_verdicts
              (id, symbol, trading_day, verdict, confidence, rationale,
               headline_count, headline_digest, suggested_structure, position_action)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (symbol, trading_day) DO UPDATE SET
              verdict=EXCLUDED.verdict, confidence=EXCLUDED.confidence,
              rationale=EXCLUDED.rationale, headline_count=EXCLUDED.headline_count,
              headline_digest=EXCLUDED.headline_digest,
              suggested_structure=EXCLUDED.suggested_structure,
              position_action=EXCLUDED.position_action, updated_at=now()
            """,
            (str(uuid.uuid4()), sym, day, v, res["confidence"], res["rationale"],
             res["headline_count"], d, structure, act),
        )
        print(f"{sym:6s} {v:14s} {res['confidence']:5.2f} {len(heads):3d} "
              f"{structure:20s} {act}")
        if res["rationale"]:
            print(f"       -> {res['rationale'][:150]}")

    print("\nADVISORY ONLY. Nothing here places or closes an order. On the 365 "
          "labelled rows in news_symbol_impact, news mentions do not yet predict "
          "a move; re-check before this is allowed to gate anything.")
    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
