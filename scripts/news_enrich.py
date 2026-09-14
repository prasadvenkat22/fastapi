"""Macro news enrichment.

    RSS -> feedparser -> dedupe -> text extraction -> FinBERT + spaCy
           (in parallel) -> combined record -> JSONL + macro verdict

WHAT THIS IS FOR. Polygon supplies per-ticker sentiment and structurally cannot
supply a macro read -- measured 2026-09-12, `ticker=QQQ` returns ETF
comparisons ("Forget JEPQ...") because that is what an ETF ticker feed carries,
and market-wide news matched 0 of the 114 MACRO_TERMS. So the macro tape comes
from general wires and something has to score it. This is that job.

WHY FINBERT WORKS HERE AND DID NOT WORK FOR TICKERS. FinBERT is a SENTENCE
classifier. Pointed at a ticker it cannot know WHICH company it is scoring, and
on a multi-ticker article it scored the wrong one -- the case that removed it
on 2026-09-12 (commit 4ea2e30f7):

    "Nike Is Being Deleted From the S&P 100. Is Its Seat in the Dow
     Jones Industrial Average in Jeopardy?"

    polygon  positive   correct -- SanDisk is being ADDED
    finbert  -0.81      read "Deleted... in Jeopardy?"

A MACRO READ HAS NO ATTRIBUTION STEP TO GET WRONG. "Fed holds rates, signals
two cuts" is risk-on for the whole tape, which is precisely the question
FinBERT was trained on. The old failure mode cannot occur here.

NER IS A FILTER, NOT A SCORER. spaCy decides which stories are macro at all --
a Fed/Treasury/OPEC story is, a single-company story is not however bearish it
reads. Without it the macro tape fills with single-name news and the QQQ
verdict becomes an average of whichever companies were in the news that hour,
which is not a macro read. The entities are also kept on every record, because
they are the raw material for any later per-entity work.

NOTHING HERE TRADES. It writes a macro row and a JSONL record; news_watch.py
turns that into the verdict the gates read.

    python scripts/news_enrich.py                # fetch, score, store
    python scripts/news_enrich.py --dry-run      # score and print, store nothing
    python scripts/news_enrich.py --report
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import psycopg2

logger = logging.getLogger("news_enrich")
NY = ZoneInfo("America/New_York")

# THE MACRO WIRES. The Fed feed is the only PRIMARY source that works from
# here, and it is the most important one: a Fed statement is the event rather
# than a report of the event, and it lands before the wires rewrite it. The
# rest carry the tape's reaction.
#
# EVERY URL BELOW WAS FETCHED FROM THE DROPLET ON 2026-09-13 AND RETURNED
# ENTRIES. Four of the originally proposed feeds did not, and are recorded here
# so they are not re-added on the assumption that they work:
#
#   bls.gov/feed/bls_latest.rss      0 entries   (news_release.rss and
#   bls.gov/feed/news_release.rss    0 entries    home.rss also 0 from this box)
#   feeds.reuters.com/...businessNews    0       Reuters retired public RSS
#   feeds.reuters.com/...USMarketsNews   0       Reuters retired public RSS
#   home.treasury.gov/rss/press.xml  timeout
#   mw_realtimeheadlines            10 entries, NEWEST 2025-06-11 -- fifteen
#                                   months stale. This is the mw_marketpulse
#                                   failure again: a feed that returns rows and
#                                   has stopped publishing reads as a QUIET
#                                   TAPE, not a broken one. fetch() warns on
#                                   anything older than three days for exactly
#                                   this reason; it was caught by that check.
#
# So jobs and rate releases arrive here second-hand, via the wires, rather than
# from BLS directly. The Fed feed is the one primary source that works, and it
# is the most important one.
FEEDS = [
    ("federal-reserve", "https://www.federalreserve.gov/feeds/press_all.xml"),
    ("cnbc-finance", "https://search.cnbc.com/rs/search/combinedcms/view.xml"
                     "?partnerId=wrss01&id=100003114"),
    ("cnbc-economy", "https://www.cnbc.com/id/20910258/device/rss/rss.html"),
    ("marketwatch-top", "https://feeds.content.dowjones.io/public/rss/mw_topstories"),
    ("marketwatch-bulletins",
     "https://feeds.content.dowjones.io/public/rss/mw_bulletins"),
    ("investing-economy", "https://www.investing.com/rss/news_14.rss"),
    ("investing-all", "https://www.investing.com/rss/news.rss"),
    ("yahoo-finance", "https://finance.yahoo.com/news/rssindex"),
]

# "This story is about the economy, not a company." ORG alone cannot decide it
# -- every company story has an ORG and most carry MONEY and PERCENT, so a
# type-based rule keeps everything, which is the failure this filter exists to
# prevent. A story qualifies on a macro INSTITUTION or a macro KEYWORD.
MACRO_ORGS = {
    "fed", "federal reserve", "fomc", "treasury", "ecb", "boj", "bank of japan",
    "bank of england", "imf", "world bank", "opec", "opec+", "bls",
    "bureau of labor statistics", "cbo", "congress", "senate", "white house",
    "supreme court", "sec", "cftc", "nato", "eia",
}
MACRO_KEYWORDS = {
    "inflation", "cpi", "ppi", "pce", "rate cut", "rate hike", "interest rate",
    "yield", "yields", "treasury", "bond", "jobs report", "payrolls",
    "unemployment", "jobless", "gdp", "recession", "tariff", "tariffs",
    "trade war", "sanctions", "oil", "crude", "opec", "dollar", "deficit",
    "shutdown", "debt ceiling", "stimulus", "quantitative", "hawkish",
    "dovish", "soft landing", "stagflation", "vix", "volatility",
}

# en_core_web_trf as specified. IT IS THE MEMORY RISK ON THIS BOX, not the
# speed cost: trf is a transformer (~450MB) and FinBERT is another (~440MB),
# and with weights plus activations the pair lands near 2GB against the ~2.6GB
# free while the trading engine is running. An OOM here would be killed by the
# kernel, and the kernel does not promise to kill THIS process rather than the
# engine. Run it as a one-shot job (memory returns on exit) and drop to
# en_core_web_sm via this env var if the box complains -- sm is ~50MB and this
# is a keep/drop decision on short text, not fine-grained entity linking.
SPACY_MODEL = os.getenv("TRADING_SPACY_MODEL", "en_core_web_trf")
FINBERT_MODEL = os.getenv("TRADING_FINBERT_MODEL", "ProsusAI/finbert")
MACRO_SYMBOL = os.getenv("TRADING_MACRO_SYMBOL", "QQQ")
LOOKBACK_HOURS = int(os.getenv("TRADING_MACRO_LOOKBACK_H", "24"))
OUT_PATH = os.getenv("TRADING_NEWS_ENRICHED_PATH", "/app/data/news_enriched.jsonl")

# How many macro headlines a verdict needs. One risk-off story is not a
# risk-off tape, and a mean over two swings on either of them.
MIN_ARTICLES = int(os.getenv("TRADING_MACRO_MIN_ARTICLES", "4"))

# FinBERT truncates at 512 tokens; title + summary is well inside that.
MAX_CHARS = int(os.getenv("TRADING_ENRICH_MAX_CHARS", "1200"))

_nlp = None
_finbert = None


def _dsn() -> str:
    url = os.getenv("DATABASE_URL", "")
    return url.replace("postgresql+psycopg2://", "postgresql://").replace(
        "postgresql+asyncpg://", "postgresql://")


# --------------------------------------------------------------- models


def nlp():
    global _nlp
    if _nlp is None:
        try:
            import spacy
        except ImportError:
            raise RuntimeError("spacy is not installed. pip install spacy")
        try:
            _nlp = spacy.load(SPACY_MODEL)
        except OSError:
            raise RuntimeError(f"spaCy model {SPACY_MODEL} not downloaded. "
                               f"python -m spacy download {SPACY_MODEL}")
    return _nlp


def finbert():
    global _finbert
    if _finbert is None:
        try:
            from transformers import pipeline
        except ImportError:
            raise RuntimeError("transformers is not installed. "
                               "pip install transformers torch")
        _finbert = pipeline("sentiment-analysis", model=FINBERT_MODEL,
                            truncation=True, max_length=512)
    return _finbert


# --------------------------------------------------------------- 1. fetch


def fetch() -> list:
    """RSS -> feedparser. Every feed in its own try; one down never stops the sweep."""
    import feedparser

    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    out = []
    for name, url in FEEDS:
        try:
            entries = feedparser.parse(url).entries or []
        except Exception:
            logger.warning("%-22s unreachable", name, exc_info=True)
            continue
        if not entries:
            logger.warning("%-22s 0 entries -- feed may be dead", name)
            continue
        newest, kept = None, 0
        for e in entries:
            title = (e.get("title") or "").strip()
            if not title:
                continue
            pub = None
            for key in ("published_parsed", "updated_parsed"):
                if e.get(key):
                    pub = datetime(*e[key][:6], tzinfo=timezone.utc)
                    break
            if pub and (newest is None or pub > newest):
                newest = pub
            if pub and pub < cutoff:
                continue
            out.append({
                # GUID first, link second, title last -- the feed's own id is
                # the only one stable across a headline being edited.
                "guid": (e.get("id") or e.get("guid") or e.get("link")
                         or title)[:500],
                "title": title,
                "summary": (e.get("summary") or e.get("description") or "")[:2000],
                "link": (e.get("link") or "")[:500],
                "source": name,
                "published": pub,
            })
            kept += 1
        # A DEAD FEED READS AS QUIET, NOT BROKEN -- the failure mw_marketpulse
        # produced on 2026-09-12. Say it out loud.
        if newest and (datetime.now(timezone.utc) - newest) > timedelta(days=3):
            logger.warning("%-22s newest item %s -- STALE, check the URL",
                           name, newest.date())
        logger.info("%-22s %3d entries, %2d inside %dh", name, len(entries),
                    kept, LOOKBACK_HOURS)
    return out


# --------------------------------------------------------------- 2. dedupe


def dedupe(articles: list, persist: bool = True) -> list:
    """Drop anything whose GUID has been seen, in this run or a previous one.

    ON GUID, NOT TITLE. The wires re-publish the same story under a lightly
    edited headline all day; a title check treats each edit as a new event and
    the macro mean is then dominated by whichever story got rewritten most.
    The feed's own id survives the rewrite.

    Falls back to in-run dedupe if the table is unreachable -- a store outage
    must not stop the sweep, and a duplicate is cheaper than no macro read.
    """
    seen_run, staged = set(), []
    for a in articles:
        if a["guid"] in seen_run:
            continue
        seen_run.add(a["guid"])
        staged.append(a)
    if not persist or not staged:
        return staged
    try:
        conn = psycopg2.connect(_dsn())
        conn.autocommit = True
        with conn, conn.cursor() as cur:
            cur.execute("SELECT guid FROM news_seen WHERE guid = ANY(%s)",
                        ([a["guid"] for a in staged],))
            known = {r[0] for r in cur.fetchall()}
            fresh = [a for a in staged if a["guid"] not in known]
            if fresh:
                cur.executemany(
                    "INSERT INTO news_seen (guid, source, title, first_seen) "
                    "VALUES (%s,%s,%s,now()) ON CONFLICT (guid) DO NOTHING",
                    [(a["guid"], a["source"], a["title"][:500]) for a in fresh])
        conn.close()
        logger.info("dedupe: %d new, %d already seen", len(fresh),
                    len(staged) - len(fresh))
        return fresh
    except Exception:
        logger.warning("news_seen unreachable -- in-run dedupe only",
                       exc_info=True)
        return staged


# --------------------------------------------------- 3. text extraction


def extract_text(a: dict) -> str:
    """title + summary as one string, tags stripped.

    Both models see the SAME text, which is what makes the two halves of a
    record comparable: an entity spaCy found is an entity FinBERT also read.
    """
    import re

    body = re.sub(r"<[^>]+>", " ", a.get("summary") or "")
    body = re.sub(r"\s+", " ", body).strip()
    text = a["title"] if not body else f"{a['title']}. {body}"
    return text[:MAX_CHARS]


# ------------------------------------------- 4. FinBERT + spaCy, parallel


def enrich(articles: list) -> list:
    """Run both models over the same text, concurrently, and merge.

    Genuinely concurrent: torch and spaCy both release the GIL during
    inference, so two threads overlap rather than interleave. The cost is that
    both sets of weights are resident at once -- see SPACY_MODEL.
    """
    texts = [extract_text(a) for a in articles]
    for a, t in zip(articles, texts):
        a["text"] = t

    def run_ner():
        return [[{"text": e.text, "label": e.label_} for e in doc.ents]
                for doc in nlp().pipe(texts, batch_size=16)]

    def run_sentiment():
        return finbert()(texts)

    with ThreadPoolExecutor(max_workers=2) as pool:
        f_ner, f_sent = pool.submit(run_ner), pool.submit(run_sentiment)
        ents, sents = f_ner.result(), f_sent.result()

    for a, e, s in zip(articles, ents, sents):
        label, conf = str(s["label"]).lower(), float(s["score"])
        a["entities"] = e
        a["sentiment"] = label
        a["sentiment_conf"] = conf
        # Signed, so the macro mean is directional. Neutral contributes 0
        # rather than being dropped -- a genuinely neutral tape should read
        # neutral, not be decided by its two non-neutral items.
        a["score"] = conf if label == "positive" else -conf if label == "negative" else 0.0
        a["is_macro"], a["macro_why"] = classify_macro(a, e)
    return articles


def classify_macro(a: dict, ents: list) -> tuple:
    low = (a["title"] + " " + (a.get("summary") or "")).lower()
    names = {e["text"].lower() for e in ents
             if e["label"] in ("ORG", "GPE", "NORP")}
    hit = sorted(o for o in MACRO_ORGS if o in names or o in low)
    kw = sorted(k for k in MACRO_KEYWORDS if k in low)
    if hit or kw:
        return True, ", ".join((hit + kw)[:4])
    return False, ""


# --------------------------------------------------------------- 5. output


def macro_verdict(scored: list) -> "tuple | None":
    if len(scored) < MIN_ARTICLES:
        logger.info("only %d macro article(s), below the %d minimum -- no verdict",
                    len(scored), MIN_ARTICLES)
        return None
    vals = [a["score"] for a in scored]
    mean = sum(vals) / len(vals)
    label = "positive" if mean > 0.15 else "negative" if mean < -0.15 else "neutral"
    return label, mean, len(scored)


def store(verdict: tuple, now: datetime) -> int:
    label, mean, n = verdict
    conn = psycopg2.connect(_dsn())
    conn.autocommit = True
    with conn, conn.cursor() as cur:
        cur.execute("""
            INSERT INTO symbol_sentiment_hourly
                (symbol, asof, trading_day, source, label, score,
                 headline_count, rationale)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (symbol, source, asof) DO NOTHING
        """, (MACRO_SYMBOL, now.replace(minute=0, second=0, microsecond=0),
              now.date(), "polygon", label, mean, n,
              f"finbert macro mean {mean:+.2f} over {n} macro headline(s)"))
        written = cur.rowcount
    conn.close()
    return written


def write_jsonl(articles: list) -> int:
    """One combined record per article: metadata + sentiment + entities."""
    try:
        d = os.path.dirname(OUT_PATH)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(OUT_PATH, "a", encoding="utf-8") as fh:
            for a in articles:
                rec = dict(a)
                if isinstance(rec.get("published"), datetime):
                    rec["published"] = rec["published"].isoformat()
                rec["enriched_at"] = datetime.now(timezone.utc).isoformat()
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return len(articles)
    except Exception:
        logger.warning("Could not write %s", OUT_PATH, exc_info=True)
        return 0


def report() -> None:
    conn = psycopg2.connect(_dsn())
    with conn, conn.cursor() as cur:
        cur.execute("""
            SELECT to_char(asof AT TIME ZONE 'America/New_York','MM-DD HH24:MI'),
                   label, round(score::numeric,2), headline_count, rationale
            FROM symbol_sentiment_hourly
            WHERE symbol=%s AND asof >= now() - interval '48 hours'
            ORDER BY asof DESC LIMIT 20
        """, (MACRO_SYMBOL,))
        rows = cur.fetchall()
    conn.close()
    if not rows:
        print(f"no {MACRO_SYMBOL} macro rows in the last 48 hours")
        return
    print(f"{'when':12s} {'label':9s} {'score':>6s} {'n':>4s}  rationale")
    for when, lab, sc, n, why in rows:
        print(f"{when:12s} {lab or '-':9s} {sc:>6} {n:4d}  {(why or '')[:60]}")


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
    arts = dedupe(fetch(), persist=not args.dry_run)
    print(f"\n{len(arts)} new article(s) inside {LOOKBACK_HOURS}h")
    if not arts:
        print("nothing new to score")
        return

    arts = enrich(arts)
    macro = [a for a in arts if a["is_macro"]]
    print(f"{len(macro)} macro after the NER filter "
          f"({len(arts) - len(macro)} dropped as single-name or off-topic)\n")
    for a in sorted(macro, key=lambda x: x["score"])[:20]:
        print(f"  {a['score']:+.2f} {a['sentiment'][:3]} "
              f"[{a['macro_why'][:26]:26s}] {a['title'][:66]}")

    v = macro_verdict(macro)
    if not v:
        print("\nNo macro verdict -- too few macro headlines. NOTHING STORED, "
              "which leaves the gates on the last good verdict rather than a "
              "manufactured neutral.")
        return
    label, mean, n = v
    print(f"\nMACRO VERDICT  {label.upper()}  mean {mean:+.3f}  over {n} headline(s)")
    if args.dry_run:
        print("DRY RUN — nothing written.")
        return
    print(f"wrote {store(v, now)} row(s); "
          f"{write_jsonl(arts)} record(s) -> {OUT_PATH}")


if __name__ == "__main__":
    main()
