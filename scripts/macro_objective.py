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
import json
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

# TWO MEASURES PER CHANNEL, NOT ONE.
#
# LEVEL is the change from the session open: where the day sits.
# DRIFT is the change over the last DRIFT_MINUTES: where it is going.
#
# LEVEL ALONE CANNOT SEE AN INTRADAY REVERSAL. Yields opening at 5.00%, spiking
# to 5.10% by 11:00 and easing to 5.05% by 14:00 still read +5bp from the open,
# so they still read risk-off -- after three hours of falling. The hour that
# actually turned good for a call debit is invisible to a from-open measure,
# because the reversal never crossed back through the open.
#
# Blended rather than switched. Level carries the day's position and stops a
# single quiet hour flipping the read; drift carries the turn. 50/50 means a
# reversal registers at half strength immediately and at full strength once the
# level follows it -- which is the behaviour wanted: early, but not hair-trigger.
#
# A channel with no history yet (first run of the day, or a gap) scores on
# LEVEL alone rather than assuming zero drift, because "no reading an hour ago"
# is not "no change".
DRIFT_MINUTES = int(os.getenv("TRADING_MACRO_DRIFT_MIN", "60"))
# A prior reading younger than this is not a drift measurement, it is noise.
DRIFT_MIN_AGE = int(os.getenv("TRADING_MACRO_DRIFT_MIN_AGE", "30"))
DRIFT_WEIGHT = float(os.getenv("TRADING_MACRO_DRIFT_WEIGHT", "0.5"))

# Full-vote sizes for the DRIFT leg. Smaller than the level thresholds because
# an hour is a fraction of a session: a 1% crude move inside one hour is a
# bigger statement than 1% across the whole day.
CRUDE_DRIFT_FULL_PCT = float(os.getenv("TRADING_MACRO_CRUDE_DRIFT_PCT", "1.0"))
TNX_DRIFT_FULL_BPS = float(os.getenv("TRADING_MACRO_TNX_DRIFT_BPS", "2.5"))
VIX_DRIFT_FULL_PCT = float(os.getenv("TRADING_MACRO_VIX_DRIFT_PCT", "3.0"))


def _dsn() -> str:
    url = os.getenv("DATABASE_URL", "")
    return url.replace("postgresql+psycopg2://", "postgresql://").replace(
        "postgresql+asyncpg://", "postgresql://")


def _clamp(x: float) -> float:
    return max(-1.0, min(1.0, x))


def prior_levels() -> dict:
    """Raw levels from roughly DRIFT_MINUTES ago, for the drift leg.

    Returns (levels, minutes_ago).

    TWO GUARDS, BOTH LEARNED FROM GETTING IT WRONG FIRST.

    The row must be at least DRIFT_MIN_AGE old. Nearest-to-target alone picks
    whatever exists, and early in a session that is the row from fifteen
    minutes ago -- so "drift in 60m" would actually be a quarter hour's move,
    labelled and scaled as an hour's.

    And the ACTUAL age comes back with it, so the caller scales the threshold
    by how much time really elapsed. A 30-minute move judged against a
    60-minute bar reads as half the move it is.
    """
    try:
        conn = psycopg2.connect(_dsn())
        with conn, conn.cursor() as cur:
            cur.execute("""
                SELECT raw, EXTRACT(EPOCH FROM (now() - asof))/60.0
                FROM symbol_sentiment_hourly
                WHERE symbol=%s AND source=%s AND raw IS NOT NULL
                  AND trading_day = CURRENT_DATE
                  AND asof <= now() - (%s || ' minutes')::interval
                ORDER BY abs(EXTRACT(EPOCH FROM
                          (asof - (now() - (%s || ' minutes')::interval))))
                LIMIT 1
            """, (MACRO_SYMBOL, SOURCE, DRIFT_MIN_AGE, DRIFT_MINUTES))
            row = cur.fetchone()
        conn.close()
        return ((row[0] or {}), float(row[1])) if row else ({}, 0.0)
    except Exception:
        logger.warning("No prior levels — scoring on level alone.",
                       exc_info=True)
        return {}, 0.0


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

    prior, prior_age = prior_levels()
    ch: dict = {}
    raw: dict = {}

    def add(name, level_move, full, drift_full, now_level, unit):
        """One channel: level vote, drift vote, blended. Signed risk-on positive."""
        raw[name] = now_level
        lvl = _clamp(-level_move / full)
        was = prior.get(name)
        if was is None or not prior_age:
            ch[name] = (lvl, f"{level_move:+.2f}{unit} from open (no prior)")
            return
        # Scale the drift bar to the time that actually elapsed, so a 30-minute
        # gap is judged against 30 minutes' worth of movement.
        scale = max(0.25, min(2.0, prior_age / DRIFT_MINUTES))
        # bp channels are already in the unit we want; % channels need the
        # move expressed against the prior level, not the open.
        d_raw = (now_level - was) if unit == "bp" else (
            (now_level - was) / was * 100.0 if was else 0.0)
        drift = _clamp(-d_raw / (drift_full * scale))
        ch[name] = (lvl * (1 - DRIFT_WEIGHT) + drift * DRIFT_WEIGHT,
                    f"{level_move:+.2f}{unit} from open, {d_raw:+.2f}{unit} in "
                    f"{prior_age:.0f}m")

    try:
        oil = fetch_oil()
        if oil is not None:
            add("crude", oil.change_pct, CRUDE_FULL_PCT, CRUDE_DRIFT_FULL_PCT,
                oil.level, "%")
    except Exception:
        logger.warning("crude unreadable", exc_info=True)
    try:
        tnx = fetch_tnx()
        if tnx is not None:
            # stored as bp-equivalent so the drift arithmetic is in basis points
            add("rates", tnx.change_bps, TNX_FULL_BPS, TNX_DRIFT_FULL_BPS,
                tnx.level * 100.0, "bp")
    except Exception:
        logger.warning("10Y unreadable", exc_info=True)
    try:
        vix = fetch_vix()
        if vix is not None:
            add("vix", vix.change_pct, VIX_FULL_PCT, VIX_DRIFT_FULL_PCT,
                vix.level, "%")
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
    return score, label, ch, raw


def store(score: float, label: str, ch: dict, raw: dict,
          now: datetime) -> int:
    conn = psycopg2.connect(_dsn())
    conn.autocommit = True
    detail = " ".join(f"{k}{v[0]:+.2f}({v[1]})" for k, v in sorted(ch.items()))
    with conn, conn.cursor() as cur:
        cur.execute("""
            INSERT INTO symbol_sentiment_hourly
                (symbol, asof, trading_day, source, label, score,
                 headline_count, rationale, raw)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (symbol, source, asof) DO UPDATE SET
                label=EXCLUDED.label, score=EXCLUDED.score,
                rationale=EXCLUDED.rationale, raw=EXCLUDED.raw
        """, (MACRO_SYMBOL, now.replace(second=0, microsecond=0), now.date(),
              SOURCE, label, score, len(ch), detail, json.dumps(raw)))
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
    score, label, ch, raw = r
    print(f"OBJECTIVE MACRO  {label.upper()}  score {score:+.3f}  "
          f"({now:%H:%M %Z})")
    for k, (v, why) in sorted(ch.items()):
        arrow = "risk-on " if v > 0 else "risk-off" if v < 0 else "flat    "
        print(f"   {k:<7} {why:<34}  ->  {v:+.2f}  {arrow}")
    if args.dry_run:
        print("DRY RUN — nothing written.")
        return
    print(f"wrote {store(score, label, ch, raw, now)} row(s)")


if __name__ == "__main__":
    main()
