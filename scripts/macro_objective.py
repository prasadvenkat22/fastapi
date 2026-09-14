"""The macro read built from PRICES, not from text.

    crude + 10Y + VIX  ->  one risk-on / risk-off score  ->  stored, not gated

WHY THIS EXISTS. On 2026-09-14 the text-based macro verdict printed -0.67 at
09:00, 10:00, 11:00, 12:00, 13:00, 14:00 and 15:00 ET -- seven identical
readings -- while the tape turned hard around 11:00 and QQQ rose 1.13% off it.
The three objective series called that turn while it was happening:

    10:45 ET   crude +0.13%   10Y +2.9bp   VIX +0.57%    risk-off
    11:35 ET   crude -0.38%   10Y  0.0bp   VIX -2.87%    turning
    12:00 ET   crude -0.91%   10Y -3.2bp   VIX -3.73%    risk-on
    14:20 ET   crude -2.83%   10Y -1.8bp   VIX -3.27%

A headline read is a description of what happened; these are the transmission
channels themselves. Crude feeds inflation expectations, which feed the long
end, which is the discount rate on the longest-duration equity index there is.
VIX prices the fear directly. They cannot invert the way a sentence classifier
inverts, and they move continuously rather than in hourly text batches.

THIS GATES NOTHING, AND THAT IS DELIBERATE. Two reasons, and the second is the
one that matters:

  1. One day is one day. Today it was right and the text read was wrong; that
     is an anecdote until there are rows.

  2. THESE EXACT SERIES HAVE ALREADY BEEN MEASURED AS GATES AND THEY LOST.
     nodes.py records it: crude at every threshold that blocks anything makes
     results worse -- at +0.5% the ten entries it rejects average +0.15/trade
     against -3.48 for the twenty-six it keeps, so it filters out the better
     half. TNX at 2bp made results worse too (-7.34/tr against -2.31 ungated),
     and at the live 4bp it has never fired on a trade in 49 sessions.

     THAT IS NOT THE SAME TEST AS THIS. Those were ONE-SIDED RISK-OFF FILTERS
     on MORNING entries: refuse a long when crude spikes. This is a two-sided
     DIRECTIONAL READ updated intraday, which is a different claim and has
     never been tested. But the prior is a warning, not encouragement, and it
     is the reason this writes a row instead of refusing a trade.

macro_outcome.py is what will settle it: rows here, realized QQQ moves there.

    python scripts/macro_objective.py            # read, store
    python scripts/macro_objective.py --dry-run
    python scripts/macro_objective.py --report
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import psycopg2

logger = logging.getLogger("macro_objective")
NY = ZoneInfo("America/New_York")

MACRO_SYMBOL = os.getenv("TRADING_MACRO_SYMBOL", "QQQ")
SOURCE = "objective"

# WHAT COUNTS AS A FULL VOTE IN EACH CHANNEL.
#
# Chosen from what these instruments actually do in a session, not from what a
# daily chart makes them look like -- the same mistake that shipped an 8bp TNX
# gate which could never fire, because the 10Y's intraday peak never exceeded
# 4.3bp on any session in a month of 5-minute history.
#
#   crude  a typical session moves it under 1%; 2% is a real move
#   10Y    typical 3-6bp; 4bp is the threshold the engine already uses
#   VIX    typical 2-3%; 5% is a genuine repricing of fear
#
# Each channel is scaled and CLAMPED to [-1, 1], so one violent series cannot
# carry the read on its own -- the failure a mean of unbounded confidences had
# in the text pipeline.
CRUDE_FULL_PCT = float(os.getenv("TRADING_MACRO_CRUDE_FULL_PCT", "2.0"))
TNX_FULL_BPS = float(os.getenv("TRADING_MACRO_TNX_FULL_BPS", "4.0"))
VIX_FULL_PCT = float(os.getenv("TRADING_MACRO_VIX_FULL_PCT", "5.0"))

# How lopsided the three channels must be to call a direction rather than
# "mixed". 0.34 means at least one channel fully committed, or two half.
MARGIN = float(os.getenv("TRADING_MACRO_OBJ_MARGIN", "0.34"))


def _dsn() -> str:
    url = os.getenv("DATABASE_URL", "")
    return url.replace("postgresql+psycopg2://", "postgresql://").replace(
        "postgresql+asyncpg://", "postgresql://")


def _clamp(x: float) -> float:
    return max(-1.0, min(1.0, x))


def read() -> "tuple | None":
    """(score, label, per-channel dict) or None if nothing could be read.

    ALL THREE ARE SIGNED SO THAT POSITIVE MEANS RISK-ON for a long equity
    position. Each raw series is the opposite -- rising crude, rising yields
    and rising VIX all hurt -- so each is negated once, here, where it can be
    seen. That sign is the entire economic content of this file and it is the
    thing FinBERT could not do.
    """
    try:
        from trading_engine.data_feed import fetch_oil, fetch_tnx, fetch_vix
    except Exception:
        logger.warning("data_feed unavailable", exc_info=True)
        return None

    ch: dict = {}
    try:
        oil = fetch_oil()
        if oil is not None:
            ch["crude"] = (_clamp(-oil.change_pct / CRUDE_FULL_PCT),
                           f"{oil.change_pct:+.2f}%")
    except Exception:
        logger.warning("crude unreadable", exc_info=True)
    try:
        tnx = fetch_tnx()
        if tnx is not None:
            ch["rates"] = (_clamp(-tnx.change_bps / TNX_FULL_BPS),
                           f"{tnx.change_bps:+.1f}bp")
    except Exception:
        logger.warning("10Y unreadable", exc_info=True)
    try:
        vix = fetch_vix()
        if vix is not None:
            ch["vix"] = (_clamp(-vix.change_pct / VIX_FULL_PCT),
                         f"{vix.change_pct:+.2f}%")
    except Exception:
        logger.warning("VIX unreadable", exc_info=True)

    if not ch:
        return None
    # MEAN OF WHAT COULD BE READ, not of three. A feed being down must not read
    # as that channel being neutral -- that would drag every score toward zero
    # and make an outage look like calm.
    score = sum(v for v, _ in ch.values()) / len(ch)
    label = ("positive" if score > MARGIN
             else "negative" if score < -MARGIN else "neutral")
    return score, label, ch


def store(score: float, label: str, ch: dict, now: datetime) -> int:
    conn = psycopg2.connect(_dsn())
    conn.autocommit = True
    detail = " ".join(f"{k}{v[0]:+.2f}({v[1]})" for k, v in sorted(ch.items()))
    with conn, conn.cursor() as cur:
        cur.execute("""
            INSERT INTO symbol_sentiment_hourly
                (symbol, asof, trading_day, source, label, score,
                 headline_count, rationale)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (symbol, source, asof) DO UPDATE SET
                label=EXCLUDED.label, score=EXCLUDED.score,
                rationale=EXCLUDED.rationale
        """, (MACRO_SYMBOL, now.replace(second=0, microsecond=0), now.date(),
              SOURCE, label, score, len(ch), detail))
        n = cur.rowcount
    conn.close()
    return n


def report() -> None:
    """Both macro reads side by side, which is the whole point of storing this."""
    conn = psycopg2.connect(_dsn())
    with conn, conn.cursor() as cur:
        cur.execute("""
            SELECT to_char(asof AT TIME ZONE 'America/New_York','MM-DD HH24:MI'),
                   source, label, round(score::numeric,2), rationale
            FROM symbol_sentiment_hourly
            WHERE symbol=%s AND source IN ('objective','finbert')
              AND asof >= now() - interval '48 hours'
            ORDER BY asof DESC, source
        """, (MACRO_SYMBOL,))
        rows = cur.fetchall()
    conn.close()
    if not rows:
        print("no macro rows in the last 48 hours")
        return
    print(f"{'when':12s} {'source':10s} {'label':9s} {'score':>6s}  detail")
    for when, src, lab, sc, why in rows:
        print(f"{when:12s} {src:10s} {lab or '-':9s} {sc:>6}  {(why or '')[:52]}")
    print("\nNEITHER GATES ANYTHING. 'finbert' is the text read (Gemini-scored "
          "RSS, despite the source name); 'objective' is crude/10Y/VIX. They "
          "are stored side by side so macro_outcome.py can score both against "
          "the session that followed.")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()

    if args.report:
        report()
        return

    now = datetime.now(NY)
    r = read()
    if not r:
        print("no objective macro read available — nothing stored.")
        return
    score, label, ch = r
    print(f"OBJECTIVE MACRO  {label.upper()}  score {score:+.3f}  "
          f"({now:%H:%M %Z})")
    for k, (v, raw) in sorted(ch.items()):
        arrow = "risk-on " if v > 0 else "risk-off" if v < 0 else "flat    "
        print(f"   {k:<7} {raw:>9}  ->  {v:+.2f}  {arrow}")
    if args.dry_run:
        print("DRY RUN — nothing written.")
        return
    print(f"wrote {store(score, label, ch, now)} row(s)")


if __name__ == "__main__":
    main()
