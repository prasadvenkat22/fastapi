"""Per-symbol news watcher: grade the news that broke since the last close,
decide a structure, review what is already open.

THE WINDOW IS SINCE THE PREVIOUS SESSION'S CLOSE, not today's calendar date.
Corrected 2026-09-07: a calendar filter asks for "headlines stamped today" and
so throws away exactly the headlines that matter at 09:30 -- the after-hours
and weekend catalysts the market has not traded on yet. SanDisk's S&P 100
inclusion published Friday 22:11 ET was invisible to the old filter on Monday
morning.

RUNS HOURLY, 09:20-16:00 ET (TRADING_NEWS_HOURLY, on by default). It used to
run once at 09:30, on the reasoning that the verdict sets the day's structure
and the structure is decided once. That was true of a book that entered in the
morning and held. It stopped being true when rotation shipped: dte0_trade can
open a new position at noon, and it was vetoing that entry against a verdict
graded three hours earlier. SanDisk's 10:08 catalyst on 2026-09-11 was never
graded at all.

RE-RUNNING IS NEARLY FREE. A digest of the headline set is stored beside the
verdict, so an hour with no new headlines costs one query and no model call.
Only a genuinely new headline pays for a re-grade, which is the event worth
paying for. Set TRADING_NEWS_HOURLY=false to restore the 09:20-10:05 window.

WHAT IT DOES AND DOES NOT DO. It writes a verdict, a suggested structure, and
an action for any open position in that name. IT DOES NOT TRADE. On the 365
labelled rows this repository now has, being in the news predicts nothing --
the means are inside +/-0.6 ATR with larger standard deviations, and 35% of
the sample is one trending name. Section 22 (crude) and section 14 (the macro
LLM verdict) are the precedent: a term that has never been measured is logged
beside the decision, not wired into it. When news_symbol_impact has enough
rows to say whether VERY_BULLISH actually precedes a move, that is the moment
to consider gating.

    python scripts/news_watch.py            # all tracked symbols (09:20-16:00 ET)
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
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import psycopg2

from trading_engine.symbol_news import (ALIASES, classify_day,
                                        previous_session_close,
                                        session_headlines)

NY = ZoneInfo("America/New_York")

# Re-grade through the session rather than once at the open. Off would restore
# the original 09:20-10:05 behaviour; on is what rotation needs, and the
# headline digest keeps an unchanged hour free.
HOURLY = os.getenv("TRADING_NEWS_HOURLY", "true").lower() == "true"

# How stale the corpus may be before this job fetches for itself. The hourly
# fetcher runs every 60 minutes, so 90 tolerates one late run and catches a
# job that has actually stopped.
INGEST_MAX_AGE_MIN = float(os.getenv("TRADING_NEWS_MAX_CORPUS_AGE_MIN", "90"))

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


def corpus_age() -> "float | None":
    """Minutes since the newest stored headline, or None if the store is empty."""
    try:
        conn = psycopg2.connect(_dsn())
        with conn, conn.cursor() as cur:
            cur.execute("SELECT extract(epoch from (now() - max(publication_date)))/60 "
                        "FROM market_news_vectors")
            row = cur.fetchone()
        conn.close()
        return float(row[0]) if row and row[0] is not None else None
    except Exception as exc:
        print(f"(could not read corpus age: {exc})")
        return None


def ingest() -> int:
    """Top the corpus up ONLY IF THE HOURLY JOB HAS NOT.

    THIS IS A SAFETY NET, NOT THE FETCHER. news_hourly.py is the only fetcher
    -- it pulls Polygon per ticker plus the macro tape and writes
    market_news_vectors, which is what session_headlines and therefore the
    verdict read. This function used to call nodes._scrape_headlines(), and
    that function was deleted with the RSS machinery. The call sat inside a
    bare `except Exception`, so it printed a one-line failure and returned 0
    and the morning read went on grading whatever happened to be stored. A
    silent no-op behind an except is the same bug as the empty overnight
    window it was written to fix.

    So: measure first. If the newest headline is recent, the hourly job is
    healthy and there is nothing to do -- and a sweep here would burn nine
    Polygon calls and two minutes of pacing for nothing. Only a stale corpus
    pays for a sweep, which is exactly the case where the 09:30 read would
    otherwise be blind.

    Never raises. No headlines is a real answer and a wire outage must not
    read as a signal -- the same rule classify_day() follows.
    """
    age = corpus_age()
    if age is not None and age <= INGEST_MAX_AGE_MIN:
        print(f"corpus is {age:.0f} min old — hourly job is current, no fetch")
        return 0
    stale = "empty" if age is None else f"{age:.0f} min old"
    print(f"corpus is {stale} (limit {INGEST_MAX_AGE_MIN:.0f} min) — sweeping Polygon")
    try:
        from news_hourly import sweep

        sweep()
        after = corpus_age()
        if after is not None:
            print(f"corpus now {after:.0f} min old")
        return 1
    except Exception as exc:
        print(f"(sweep failed, grading on what is already stored: {exc})")
        return 0


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
        # A MORNING WINDOW, not the whole session. The verdict is read once at
        # the open to set the day's structure, so this runs once -- and the
        # crontab has to list BOTH 13:30 and 14:30 UTC to cover EDT and EST,
        # which means one of the two is always an hour late. A wide guard let
        # the late one through and the job ran twice.
        # THE WINDOW WIDENS WHEN THE VERDICT IS READ MORE THAN ONCE.
        #
        # The guard above exists because the 09:30 verdict sets the day's
        # structure and a wide window let the EST/EDT duplicate cron entry run
        # it twice. That reasoning holds for a once-a-day read and stops
        # holding the moment rotation exists: a position opened at 12:00 was
        # being vetoed against a three-hour-old verdict, which is the same
        # staleness that let SanDisk's 10:08 catalyst go ungraded on
        # 2026-09-11.
        #
        # RE-RUNNING IS CHEAP BECAUSE OF THE DIGEST. A hash of the headline
        # set is stored beside the verdict, so an hourly run with unchanged
        # headlines skips the model entirely -- it costs one query. Only a
        # genuinely new headline pays for a re-grade, which is exactly the
        # event worth paying for.
        lo, hi = dtime(9, 20), (dtime(16, 0) if HOURLY else dtime(10, 5))
        if not (lo <= now.time() <= hi):
            print(f"{now:%Y-%m-%d %H:%M %Z} — outside the "
                  f"{lo:%H:%M}-{hi:%H:%M} ET window, nothing to do.")
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

    n_new = ingest()
    # ONE asof FOR THE WHOLE SWEEP, truncated to the minute. Nine symbols
    # graded over two minutes would otherwise carry nine different
    # timestamps, and "the verdict in force at 09:30" would become a range
    # query with an off-by-one at every boundary.
    graded_at = datetime.now(NY).replace(second=0, microsecond=0)
    print(f"NEWS WATCH  {datetime.now(NY):%Y-%m-%d %H:%M %Z}  trading day {day}")
    print(f"scraped {n_new} headlines, window opens {previous_session_close(day):%a %m-%d %H:%M} ET\n")
    print(f"{'sym':6s} {'verdict':14s} {'conf':>5s} {'n':>3s} {'structure':20s} action")
    for sym in syms:
        heads = session_headlines(sym, day)
        d = digest(heads) if heads else None

        cur.execute("SELECT headline_digest, verdict, confidence, rationale "
                    "FROM news_verdicts WHERE symbol=%s AND trading_day=%s", (sym, day))
        prev = cur.fetchone()

        if not heads:
            print(f"{sym:6s} {'(no news)':14s}   nothing since the last close")
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
               headline_count, headline_digest, suggested_structure,
               position_action, asof)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (symbol, trading_day) DO UPDATE SET
              verdict=EXCLUDED.verdict, confidence=EXCLUDED.confidence,
              rationale=EXCLUDED.rationale, headline_count=EXCLUDED.headline_count,
              headline_digest=EXCLUDED.headline_digest,
              suggested_structure=EXCLUDED.suggested_structure,
              position_action=EXCLUDED.position_action,
              asof=EXCLUDED.asof, updated_at=now()
            """,
            (str(uuid.uuid4()), sym, day, v, res["confidence"], res["rationale"],
             res["headline_count"], d, structure, act, graded_at),
        )
        # AND THE APPEND-ONLY COPY. The row above is the CURRENT verdict and
        # an hourly re-grade overwrites it; this one is never rewritten, so a
        # measurement script can ask what the read was AT THE OPEN rather than
        # scoring an afternoon verdict against a move it had already seen.
        # See migration f7b3d02a5e41.
        cur.execute(
            """
            INSERT INTO news_verdict_history
              (id, symbol, trading_day, asof, verdict, confidence, rationale,
               headline_count, headline_digest, suggested_structure)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (symbol, trading_day, asof) DO NOTHING
            """,
            (str(uuid.uuid4()), sym, day, graded_at, v, res["confidence"],
             res["rationale"], res["headline_count"], d, structure),
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
