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

POLYGON'S SENTIMENT IS ASPECT-BASED, WHICH IS THE WHOLE REASON IT IS THE ONLY
ONE HERE. It reads the article body and emits a verdict PER TICKER:

    {"ticker":"NKE","sentiment":"negative",
     "sentiment_reasoning":"Stock at 12-year lows, declining revenue..."}

FinBERT was scored alongside it and removed 2026-09-12. It is a sentence
classifier, not an aspect-based one, so it was handed a bare headline with no
way to know which ticker it was rating -- and on a multi-ticker article it
rated the wrong subject. The case that settled it:

    "Nike Is Being Deleted From the S&P 100. Is Its Seat in the Dow
     Jones Industrial Average in Jeopardy?"

    polygon  positive  "Being added to S&P 100, ranked top 50 by market cap"
    finbert  -0.81     read "Deleted... in Jeopardy?"

Nike is deleted; SanDisk is ADDED. Polygon was right and FinBERT was answering
a different question. That also explains its 50-52% on the graded outcomes: it
was scoring the wrong subject a good fraction of the time. No prompt or
threshold fixes it -- there is no way to tell a sentence classifier "score
this headline FOR SanDisk".

BEING THE RIGHT SHAPE IS NOT THE SAME AS BEING RIGHT. Polygon's read has never
been scored against an outcome here either, which is why nothing gates on it.
verdict_outcome grades it nightly.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import date, datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
import psycopg2

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logger = logging.getLogger("news_hourly")
NY = ZoneInfo("America/New_York")

POLYGON_KEY = os.getenv("POLYGON_API_KEY", "")
POLYGON_NEWS = "https://api.polygon.io/v2/reference/news"
# QQQ IS NOT HERE, AND NEITHER IS ANY INDEX TICKER. Polygon's news endpoint
# does not carry macro journalism in any ticker form. Measured 2026-09-12:
#
#     ticker=QQQ    8 articles over 12 days, every one an ETF comparison
#     I:NDX         0 articles      I:SPX   0        C:USD  0
#     SPY, TLT      2 each, retail advice pieces, no macro content
#
# Index and currency identifiers belong to the aggregates API and are not
# news-taggable at all. The macro tape comes from nodes.MACRO_FEEDS instead,
# ingested at the end of this sweep. Querying QQQ here spent an API call and
# 13 seconds of pacing to fetch fund comparisons.
SYMBOLS = [s.strip().upper() for s in os.getenv(
    "TRADING_HOURLY_SYMBOLS",
    "NVDA,TSLA,AAPL,AMZN,MSFT,META,GOOGL,AVGO").split(",") if s.strip()]

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

# RECENCY WEIGHTING, NOT A SHORTER WINDOW, AND THE DATA DECIDED WHICH.
#
# This averaged every article in the 24h fetch equally, so one new story
# against twenty-three hours of yesterday's barely moved the score -- the same
# defect the macro text read had before its verdict window was cut to 4h. MU
# read -0.40 on a morning where the coverage driving it was the previous day's
# AI selloff.
#
# A HARD SHORT WINDOW IS THE WRONG FIX HERE, because ticker news is sparse in a
# way the RSS macro tape is not. Measured 2026-09-15 across nine tickers:
#
#     window   1h   4h   8h   24h
#     articles  2   15   21    42        TSLA: ONE article in 24 hours
#
# A 1h cutoff leaves almost every name ungraded and a 4h cutoff silences the
# thin ones entirely -- and "no news" then reads as neutral, which is a verdict
# nobody produced.
#
# Exponential decay keeps every article and lets age decide its weight: at a 4h
# half-life a story an hour old counts 0.84, four hours 0.50, twelve hours
# 0.125. Fresh news dominates, old news fades without vanishing, and a name
# with one article still gets a score.
HALFLIFE_HOURS = float(os.getenv("TRADING_NEWS_HALFLIFE_H", "4"))

# Sample-size shrinkage constant. 3 means one article keeps a quarter of its
# score, three keep half, fifteen keep 83%.
SHRINK_K = float(os.getenv("TRADING_NEWS_SHRINK_K", "3"))


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
    now = datetime.now(timezone.utc)
    # Hours since today's 09:30 ET open, or None outside a session.
    _session_open_age_h = None
    try:
        from zoneinfo import ZoneInfo

        ny = now.astimezone(ZoneInfo("America/New_York"))
        op = ny.replace(hour=9, minute=30, second=0, microsecond=0)
        if ny >= op:
            _session_open_age_h = (ny - op).total_seconds() / 3600.0
    except Exception:
        pass
    num = den = 0.0
    vals, reasons, ages = [], [], []
    for a in articles:
        # AGE FIRST, because it decides how much this article counts.
        age_h = None
        pub = a.get("published_utc")
        if pub:
            try:
                age_h = max(0.0, (now - datetime.fromisoformat(
                    str(pub).replace("Z", "+00:00"))).total_seconds() / 3600.0)
            except ValueError:
                age_h = None
        # AGE FROM THE OPEN, NOT FROM PUBLICATION, FOR ANYTHING PRE-MARKET.
        #
        # Wall-clock decay treats overnight news as stale, and it is not: it is
        # UNPRICED. Everything published between yesterday's close and today's
        # bell gets priced together at the open, so at 09:30 a story from 22:00
        # and one from 08:00 are equally live. Decaying them separately made a
        # 20-hour-old article worth 0.03 at a 4h half-life -- one fresh story
        # outweighing five overnight ones by six times, which would have
        # scored SNDK's Friday-22:11 S&P 100 inclusion at roughly nothing on
        # the Monday it mattered.
        #
        # So pre-open articles age from the OPEN and intraday articles age from
        # publication. Overnight news dominates early and fades through the
        # session; fresh news dominates late. That is how the tape treats them.
        eff = age_h
        if eff is not None and _session_open_age_h is not None:
            eff = min(eff, _session_open_age_h)
        w = 0.5 ** (eff / HALFLIFE_HOURS) if eff is not None else 1.0
        for ins in (a.get("insights") or []):
            if (ins.get("ticker") or "").upper() != symbol:
                continue
            s = (ins.get("sentiment") or "").lower()
            v = 1.0 if s == "positive" else -1.0 if s == "negative" else 0.0
            vals.append(v)
            num += v * w
            den += w
            if age_h is not None:
                ages.append(age_h)
            if ins.get("sentiment_reasoning") and len(reasons) < 3:
                reasons.append(f"[{s}] {ins['sentiment_reasoning']}")
    if not vals or den <= 0:
        return None
    raw = num / den

    # SHRINK TOWARD ZERO BY SAMPLE SIZE, because a mean of one article is not
    # a confident reading -- it is an unmeasured one.
    #
    # Polygon's score is the mean of per-article verdicts, so a name with ONE
    # article scores 1.00 and a name with fifteen regresses toward zero as its
    # coverage disagrees. Measured 2026-09-15: DELL 1.00 on 1 article and TSLA
    # -1.00 on 1, against NVDA 0.28 on 18 and GOOGL 0.46 on 13. The veto floor
    # then systematically preferred the LEAST-covered name, which is backwards:
    # thin coverage is the one case where a strong number means least.
    #
    # score * n/(n+k) with k=3: one article keeps a quarter of its score, three
    # keep half, fifteen keep 83%. A veto now needs volume AND agreement.
    #
    # THE RAW SCORE AND THE COUNT ARE KEPT in the raw column, so this is
    # auditable and reversible -- and so rows written before 2026-09-15, which
    # are unshrunk, can be told apart from rows written after.
    eff_n = den
    score = raw * eff_n / (eff_n + SHRINK_K)
    label = "positive" if score > 0.15 else "negative" if score < -0.15 else "neutral"
    fresh = f", newest {min(ages):.1f}h" if ages else ""
    note = (f"(recency-weighted {HALFLIFE_HOURS:.0f}h half-life{fresh}; "
            f"raw {raw:+.2f} over {len(vals)} article(s), shrunk to "
            f"{score:+.2f}) ")
    return (label, score, (note + " | ".join(reasons))[:1500], len(vals),
            {"raw_score": round(raw, 4), "n": len(vals),
             "effective_n": round(eff_n, 2), "shrink_k": SHRINK_K})


def _store_corpus(articles: list) -> None:
    """Embed and store these headlines, so the rest of the pipeline still works.

    THIS JOB IS NOW THE ONLY FETCHER. market_news_vectors feeds the novelty
    filter and session_headlines, which feeds the 09:30 verdict and
    verdict_outcome behind it. Dropping the RSS scrape without writing here
    would leave all three reading a corpus nobody tops up.

    Source and publication time travel through nodes' module globals because
    store_headlines reads them from there -- the same channel the scrape used.
    """
    import asyncio

    from trading_engine import nodes
    from trading_engine.vector_store import store_headlines
    from GENAI.vector_stores import VoyageEmbeddings

    titles = []
    for a in articles:
        t = a.get("title")
        if not t or t in titles:
            continue
        titles.append(t)
        pub = (a.get("publisher") or {}).get("name") or "POLYGON"
        nodes._LAST_SOURCES[t] = f"POLYGON:{pub}"[:60]
        # Polygon sends an ISO string; asyncpg wants a datetime, and the RSS
        # path fed it one. A string here fails the whole executemany batch.
        pub_at = a.get("published_utc")
        # Polygon sends a string; the macro tape already gives a datetime.
        if isinstance(pub_at, str):
            try:
                pub_at = datetime.fromisoformat(pub_at.replace("Z", "+00:00"))
            except ValueError:
                pub_at = None
        nodes._LAST_PUBLISHED[t] = pub_at
    if not titles:
        return
    try:
        asyncio.run(store_headlines(titles, VoyageEmbeddings()))
    except Exception:
        logger.warning("Could not store %d headlines — sentiment still recorded.",
                       len(titles), exc_info=True)


# THE RSS TICKER LEG.
#
# WHY IT EXISTS, 2026-09-16. Reuters broke that SK Hynix was in talks with
# Intel to make memory chips in the US. INTC opened +5.2% on it. CNBC's feed
# carried it and dedupe stored it at 09:12 ET, eighteen minutes before the
# bell -- and Polygon never had it: ZERO mentions of "hynix" across 618
# articles in 72 hours, and INTC's own ticker feed held six articles, the
# newest a GPU-shipments press release.
#
# So the engine graded INTC "positive +0.29" off an industry PR and two
# articles recommending other companies, while the story that moved the stock
# sat in news_seen on the macro leg.
#
# THE SPLIT WAS RIGHT AND THE ASSUMPTION UNDER IT WAS NOT. Macro from RSS,
# tickers from Polygon is a clean division -- but it assumes Polygon covers
# ticker news. It does not always, and a wire service scooping it is the
# normal case, not the exception.
#
# WHY news_seen's OWN SCORE CANNOT BE REUSED. news_enrich classifies into
# MACRO topics -- rates, growth, trade -- and anything that is not macro keeps
# rule_dir NULL. Of 175 RSS rows that day, 43 were scored, and every
# company-specific headline was among the 132 that were not. The rows are
# stored; the judgement was never made.
#
# SO THESE ARE SCORED HERE, per company, and then POOLED WITH POLYGON'S
# ARTICLES rather than averaged with Polygon's score. Pooling is what makes
# the existing machinery apply unchanged: the recency half-life, the pre-open
# ageing and the n/(n+k) shrinkage all act on the union, so one strong
# specific story sits against three generic ones and the sample-size discount
# counts them together.
#
# ONE GEMINI CALL PER SWEEP for every matched headline across every symbol,
# not one per symbol. A failure returns nothing and the sweep falls back to
# Polygon alone -- an outage must produce no opinion, never a wrong one.
#
# TRADING_NEWS_RSS_TICKER=false turns it off with a restart and no deploy.
RSS_TICKER = os.getenv("TRADING_NEWS_RSS_TICKER", "true").lower() == "true"
RSS_TICKER_MAX = int(os.getenv("TRADING_NEWS_RSS_TICKER_MAX", "60"))

_RSS_SYSTEM = (
    "You rate financial headlines for ONE named company each. For every "
    "numbered line, given as 'TICKER :: headline', decide the sentiment of "
    "that headline FOR THAT COMPANY'S SHARE PRICE. Return JSON: a list of "
    "objects with keys i (the number), s (positive, negative or neutral) and "
    "why (at most 20 words). Judge only the company named before '::'. A "
    "headline about the sector, or one where the company is listed in passing "
    "among others, is neutral unless it says something specific about that "
    "company. Advertising, sponsored content and 'top N stocks to buy' "
    "listicles are neutral."
)


def _rss_candidates(cur, since: datetime) -> list:
    """(symbol, guid, title, when) for every alias hit in the window.

    Matched on a word boundary so "intel" does not fire on "intelligence" --
    without that, every AI headline the feeds carry would read as an Intel
    story, and the feeds carry a great many.
    """
    from trading_engine.symbol_news import patterns_for

    cur.execute(
        "SELECT guid, title, first_seen, published FROM news_seen "
        "WHERE COALESCE(published, first_seen) >= %s "
        "ORDER BY COALESCE(published, first_seen) DESC",
        (since,))
    rows = cur.fetchall()
    out = []
    for sym in SYMBOLS:
        pats = [re.compile(r"(?<![a-z0-9])" + re.escape(a) + r"(?![a-z0-9])")
                for a in patterns_for(sym)]
        for guid, title, first_seen, published in rows:
            low = (title or "").lower()
            if any(p.search(low) for p in pats):
                out.append((sym, guid, title, published or first_seen))
    return out[:RSS_TICKER_MAX]


def _rss_sentiment(cands: list) -> dict:
    """{(symbol, guid): (sentiment, why)} from one Gemini call, or {}."""
    if not cands:
        return {}
    import json as _json
    import urllib.request

    key = os.getenv("GEMINI_API_KEY", "")
    if not key:
        logger.warning("GEMINI_API_KEY unset -- RSS ticker leg scores nothing.")
        return {}
    model = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")
    url = ("https://generativelanguage.googleapis.com/v1beta/models/"
           + model + ":generateContent?key=" + key)
    lines = []
    for i, (sym, _guid, title, _when) in enumerate(cands):
        lines.append(str(i) + ". " + sym + " :: " + str(title))
    body = _json.dumps({
        "systemInstruction": {"parts": [{"text": _RSS_SYSTEM}]},
        "contents": [{"parts": [{"text": chr(10).join(lines)}]}],
        "generationConfig": {"temperature": 0.0, "maxOutputTokens": 4096,
                             "responseMimeType": "application/json"},
    }).encode()
    try:
        req = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=90) as r:
            out = _json.loads(r.read())
        rows = _json.loads(out["candidates"][0]["content"]["parts"][0]["text"])
    except Exception as exc:
        logger.warning("RSS ticker scoring unavailable (%s) -- Polygon alone "
                       "this sweep.", type(exc).__name__, exc_info=True)
        return {}
    got = {}
    for row in rows:
        try:
            sym, guid, _t, _w = cands[int(row["i"])]
        except (KeyError, ValueError, IndexError, TypeError):
            continue
        sent = str(row.get("s") or "neutral").lower()
        if sent not in ("positive", "negative", "neutral"):
            sent = "neutral"
        got[(sym, guid)] = (sent, str(row.get("why") or "")[:180])
    return got


def _as_polygon_article(sym: str, title: str, when, sent: str,
                        why: str) -> dict:
    """An RSS headline wearing Polygon's shape, so one aggregator serves both.

    Deliberately NOT a second scoring path. polygon_sentiment() carries the
    half-life, the pre-open ageing and the n/(n+k) shrinkage, and every one of
    those was argued into place against a live loss. A parallel implementation
    would drift from them silently.
    """
    return {
        "title": title,
        "published_utc": (when.isoformat() if hasattr(when, "isoformat")
                          else str(when)),
        "publisher": {"name": "rss"},
        "insights": [{"ticker": sym, "sentiment": sent,
                      "sentiment_reasoning": "[rss] " + why}],
    }


def sweep(now: "datetime | None" = None) -> int:
    now = now or datetime.now(NY)
    since = now.astimezone(ZoneInfo("UTC")) - timedelta(hours=LOOKBACK_HOURS)
    written = 0
    conn = psycopg2.connect(_dsn())
    conn.autocommit = True
    # ONE EMBEDDING CALL PER SWEEP, NOT ONE PER SYMBOL. Voyage's free tier
    # allows 3 requests a minute; storing inside the loop made twelve calls in
    # two and a half minutes and every one after the third was refused. The
    # articles are accumulated and embedded once at the end instead.
    corpus: list = []
    with conn, conn.cursor() as cur:
        # MATCHED AND SCORED ONCE, BEFORE THE PER-SYMBOL LOOP. One Gemini call
        # for the whole sweep; inside the loop it would be one per symbol.
        rss_by_sym: dict = {}
        if RSS_TICKER:
            try:
                cands = _rss_candidates(cur, since)
                scored = _rss_sentiment(cands)
                for sym, guid, title, when in cands:
                    hit = scored.get((sym, guid))
                    if not hit:
                        continue
                    rss_by_sym.setdefault(sym, []).append(
                        _as_polygon_article(sym, title, when, hit[0], hit[1]))
                if cands:
                    logger.info("RSS ticker leg: %d headline(s) matched a "
                                "symbol, %d scored, across %d name(s)",
                                len(cands), len(scored), len(rss_by_sym))
            except Exception:
                # Never let the new leg take the sweep down. Polygon alone is
                # the behaviour this replaced and it is a safe fallback.
                logger.warning("RSS ticker leg failed -- Polygon alone.",
                               exc_info=True)
                rss_by_sym = {}
        for i, sym in enumerate(SYMBOLS):
            if i:
                time.sleep(PACE_SECONDS)     # between every call, not every 4th
            arts = polygon_news(sym, since)
            extra = rss_by_sym.get(sym) or []
            # POOLED, NOT AVERAGED. One aggregator sees the union, so the
            # half-life, the pre-open ageing and the shrinkage all count both
            # corpora together -- which is the point: a single specific story
            # against three generic ones should not be a separate opinion, it
            # should be part of one sample.
            arts = list(arts) + extra
            titles = [a["title"] for a in arts if a.get("title")]
            if not titles:
                logger.info("%-5s no articles in the last %dh", sym, LOOKBACK_HOURS)
                continue
            corpus.extend(arts)
            rows = []
            p = polygon_sentiment(arts, sym)
            if p:
                # THE SOURCE LABEL SAYS WHICH CORPUS PRODUCED THE NUMBER.
                # A pooled read is not a Polygon read, and calling it one
                # would make "how accurate is Polygon on this name" answer
                # itself with a different corpus for ever after.
                rows.append(("polygon+rss" if extra else "polygon",) + p)
            for source, label, score, rationale, n, meta in rows:
                if extra and isinstance(meta, dict):
                    meta = dict(meta, rss_n=len(extra),
                                polygon_n=len(arts) - len(extra))
                cur.execute("""
                    INSERT INTO symbol_sentiment_hourly
                        (symbol, asof, trading_day, source, label, score,
                         headline_count, rationale, raw)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (symbol, source, asof) DO NOTHING
                """, (sym, now.replace(minute=0, second=0, microsecond=0),
                      now.date(), source, label, score, n, rationale,
                      json.dumps(meta)))
                written += cur.rowcount
            logger.info("%-5s %2d articles%s | %s", sym, len(titles),
                        (" (+%d rss)" % len(extra)) if extra else "",
                        "  ".join(f"{s}={l} {sc:+.2f}"
                                  for s, l, sc, _, _, _ in rows) or "no score")

        # THE MACRO TAPE, which Polygon structurally cannot supply. Measured
        # 2026-09-12: ticker=QQQ returns ETF comparisons over 12 days, and
        # market-wide news matched 0 of the 114 MACRO_TERMS. QQQ's 09:30 read
        # has nothing to match without this, and TRADING_NEWS_DIRECTION
        # becomes a switch that is on and does nothing.
        try:
            from trading_engine import nodes as _n

            macro = _n.macro_headlines()
            for title, src, pub in macro:
                corpus.append({"title": title, "published_utc": pub,
                               "publisher": {"name": src}})
            logger.info("macro tape: %d headlines", len(macro))
        except Exception:
            logger.warning("Macro tape unavailable — single-name news is "
                           "unaffected.", exc_info=True)

        _store_corpus(corpus)

        # Retention. Matched to the novelty lookback -- see RETAIN_DAYS.
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
