"""Hourly per-symbol news from Polygon, scored by every candidate at once.

    python scripts/news_hourly.py            # one sweep
    python scripts/news_hourly.py --report
    python scripts/news_hourly.py --force    # ignore the market-hours check

IT SCORES. IT DOES NOT GATE. Nothing reads symbol_sentiment_hourly at entry
time, deliberately -- see the migration for what each source has and has not
been measured at. FinBERT scored 50-52% on this account's own graded outcomes
with lookahead removed, and its "negative" days averaged +0.061%. A gate on
that refuses trades at random.

WHY POLYGON REPLACES THE RSS SCRAPE. Articles arrive TICKER-TAGGED. That
deletes the alias-matching layer and the three bug classes it produced in one
evening:

    ALIASES["SNDK"] = ["sandisk","sndk"]   could not see a sector story
    SECTOR_TERMS                           matched 0 of 236 headlines
    mw_marketpulse                         answered 200 for months while
                                           serving headlines a year old

A dead RSS feed and a quiet news day are indistinguishable from inside a
scrape. A Polygon 429 is not, and this logs it.

THE RATE LIMIT IS MEASURED, NOT ASSUMED. The free tier refused the 6th call
inside two seconds and returned 429 on eight of eight when hammered. So the
pacing here is a delay BETWEEN EVERY CALL, not a sleep after each fourth --
that pattern still bursts 4 calls into one second and trips it.

AND IT KEEPS POLYGON'S OWN SENTIMENT, which arrives free with the news and
carries reasoning:

    {"ticker":"NKE","sentiment":"negative",
     "sentiment_reasoning":"Stock at 12-year lows, declining revenue..."}

That has never been scored against an outcome here. It is stored beside
FinBERT's so verdict_outcome can settle which is worth having.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import date, datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

import httpx
import psycopg2

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logger = logging.getLogger("news_hourly")
NY = ZoneInfo("America/New_York")

POLYGON_KEY = os.getenv("POLYGON_API_KEY", "")
POLYGON_NEWS = "https://api.polygon.io/v2/reference/news"
HF_TOKEN = os.getenv("HF_TOKEN", "")
FINBERT = os.getenv("TRADING_FINBERT_URL",
                    "https://router.huggingface.co/hf-inference/models/ProsusAI/finbert")

SYMBOLS = [s.strip().upper() for s in os.getenv(
    "TRADING_HOURLY_SYMBOLS",
    "QQQ,NVDA,SNDK,MU,META,AVGO,ADBE,AMZN,GOOGL,MSFT,CRWV,WDC").split(",") if s.strip()]

# Seconds between Polygon calls. 5/minute measured, so 13s leaves headroom
# without the burst that a "sleep after every fourth" pattern still produces.
PACE_SECONDS = float(os.getenv("TRADING_POLYGON_PACE", "13"))
LOOKBACK_HOURS = int(os.getenv("TRADING_HOURLY_LOOKBACK_H", "24"))
# 10 days, matching NOVELTY_LOOKBACK_DAYS exactly. That is the constraint that
# decides this number: the novelty filter asks for prior coverage over the last
# 10 days, and cutting BELOW its window turns every re-reported story into a
# fresh catalyst -- the failure that gave the QQQ read its standing bearish
# tilt. At exactly 10 the filter still gets a full window minus a few hours at
# the boundary, which degrades gracefully; below 10 it stops working.
#
# news_verdict_outcomes is unaffected: it stores the graded verdict and the
# session return, not the headlines, so the measurement survives the purge.
RETAIN_DAYS = int(os.getenv("TRADING_NEWS_RETAIN_DAYS", "10"))


def _dsn() -> str:
    return (os.getenv("DATABASE_URL", "")
            .replace("postgresql+psycopg2://", "postgresql://")
            .replace("postgresql+asyncpg://", "postgresql://"))


def polygon_news(symbol: str, since: datetime) -> list:
    """Ticker-tagged articles, newest first. [] on any failure, loudly."""
    if not POLYGON_KEY:
        logger.error("POLYGON_API_KEY is not set — no news fetched.")
        return []
    try:
        r = httpx.get(POLYGON_NEWS, timeout=30.0, params={
            "ticker": symbol,
            "published_utc.gte": since.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "limit": 50, "apiKey": POLYGON_KEY,
        })
    except Exception as exc:  # noqa: BLE001 — one symbol must not stop the sweep
        logger.warning("%s: Polygon unreachable (%s)", symbol, type(exc).__name__)
        return []
    if r.status_code == 429:
        # NAMED, because this is the failure a scrape could never report.
        logger.warning("%s: Polygon 429 — rate limited. Raise TRADING_POLYGON_PACE.",
                       symbol)
        return []
    if r.status_code != 200:
        logger.warning("%s: Polygon returned %d: %s", symbol, r.status_code,
                       r.text[:160])
        return []
    return (r.json() or {}).get("results") or []


def polygon_sentiment(articles: list, symbol: str) -> "tuple | None":
    """(label, signed score, rationale, n) from Polygon's own insights.

    The signed score counts each article's ticker-level verdict as +1/-1/0 and
    averages, so it lands on the same scale as FinBERT's and the two compare.
    """
    vals, reasons = [], []
    for a in articles:
        for ins in (a.get("insights") or []):
            if (ins.get("ticker") or "").upper() != symbol:
                continue
            s = (ins.get("sentiment") or "").lower()
            vals.append(1.0 if s == "positive" else -1.0 if s == "negative" else 0.0)
            if ins.get("sentiment_reasoning") and len(reasons) < 3:
                reasons.append(f"[{s}] {ins['sentiment_reasoning']}")
    if not vals:
        return None
    score = sum(vals) / len(vals)
    label = "positive" if score > 0.15 else "negative" if score < -0.15 else "neutral"
    return label, score, " | ".join(reasons)[:1500], len(vals)


def finbert_sentiment(titles: list) -> "tuple | None":
    """(label, signed score, None, n). top_k=3 or a batch returns only argmax."""
    if not titles or not HF_TOKEN:
        return None
    try:
        r = httpx.post(FINBERT, headers={"Authorization": f"Bearer {HF_TOKEN}"},
                       json={"inputs": titles[:60], "parameters": {"top_k": 3}},
                       timeout=60.0)
    except Exception as exc:  # noqa: BLE001
        logger.warning("FinBERT unreachable (%s)", type(exc).__name__)
        return None
    if r.status_code != 200:
        logger.warning("FinBERT returned %d: %s", r.status_code, r.text[:160])
        return None
    tot = 0.0
    n = 0
    for scores in r.json():
        if not isinstance(scores, list):
            continue
        d = {x.get("label"): x.get("score", 0.0) for x in scores if isinstance(x, dict)}
        tot += d.get("positive", 0.0) - d.get("negative", 0.0)
        n += 1
    if not n:
        return None
    score = tot / n
    label = "positive" if score > 0.15 else "negative" if score < -0.15 else "neutral"
    return label, score, None, n


def sweep(now: "datetime | None" = None) -> int:
    now = now or datetime.now(NY)
    since = now.astimezone(ZoneInfo("UTC")) - timedelta(hours=LOOKBACK_HOURS)
    written = 0
    conn = psycopg2.connect(_dsn())
    conn.autocommit = True
    with conn, conn.cursor() as cur:
        for i, sym in enumerate(SYMBOLS):
            if i:
                time.sleep(PACE_SECONDS)     # between every call, not every 4th
            arts = polygon_news(sym, since)
            titles = [a["title"] for a in arts if a.get("title")]
            if not titles:
                logger.info("%-5s no articles in the last %dh", sym, LOOKBACK_HOURS)
                continue
            rows = []
            p = polygon_sentiment(arts, sym)
            if p:
                rows.append(("polygon",) + p)
            f = finbert_sentiment(titles)
            if f:
                rows.append(("finbert",) + f)
            for source, label, score, rationale, n in rows:
                cur.execute("""
                    INSERT INTO symbol_sentiment_hourly
                        (symbol, asof, trading_day, source, label, score,
                         headline_count, rationale)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (symbol, source, asof) DO NOTHING
                """, (sym, now.replace(minute=0, second=0, microsecond=0),
                      now.date(), source, label, score, n, rationale))
                written += cur.rowcount
            logger.info("%-5s %2d articles | %s", sym, len(titles),
                        "  ".join(f"{s}={l} {sc:+.2f}"
                                  for s, l, sc, _, _ in rows) or "no score")

        # Retention. Above the novelty lookback on purpose -- see RETAIN_DAYS.
        cur.execute("DELETE FROM market_news_vectors WHERE publication_date < %s",
                    (date.today() - timedelta(days=RETAIN_DAYS),))
        if cur.rowcount:
            logger.info("retention: dropped %d headlines older than %d days",
                        cur.rowcount, RETAIN_DAYS)
    conn.close()
    logger.info("%d sentiment row(s) written", written)
    return written


def report() -> None:
    conn = psycopg2.connect(_dsn())
    with conn, conn.cursor() as cur:
        cur.execute("""
            SELECT symbol, source, label, round(score::numeric,2), headline_count,
                   to_char(asof AT TIME ZONE 'America/New_York','MM-DD HH24:MI')
            FROM symbol_sentiment_hourly
            WHERE asof >= now() - interval '12 hours'
            ORDER BY asof DESC, symbol, source LIMIT 40
        """)
        rows = cur.fetchall()
    conn.close()
    if not rows:
        print("nothing scored in the last 12 hours")
        return
    print(f"{'sym':6s} {'source':9s} {'label':9s} {'score':>6s} {'n':>4s}  when")
    for sym, src, lab, sc, n, when in rows:
        print(f"{sym:6s} {src:9s} {lab or '-':9s} {sc:>6} {n:4d}  {when}")
    print("\nSCORES ONLY. Nothing reads this table at entry time.")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    if args.report:
        report()
        return
    now = datetime.now(NY)
    if not args.force:
        from trading_engine import market_calendar

        if not market_calendar.is_trading_day(now.date()):
            print(f"{now:%Y-%m-%d %H:%M %Z} — not a trading day.")
            return
        if not (dtime(8, 0) <= now.time() <= dtime(16, 30)):
            print(f"{now:%H:%M %Z} — outside the news window.")
            return
    sweep(now)


if __name__ == "__main__":
    main()
