"""Macro news enrichment.

    RSS -> feedparser -> dedupe -> text extraction -> spaCy NER
        -> (macro only) FinBERT -> combined record -> JSONL + macro verdict

    NER GATES THE SCORER. Only stories spaCy identifies as macro are sent to
    FinBERT, and they are sent ENTITY-SCOPED -- the sentences carrying the
    macro terms, not the whole column.

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
import re
import sys
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
    # rates and prices
    "inflation", "cpi", "ppi", "pce", "rate cut", "rate hike", "interest rate",
    "yield", "yields", "treasury", "bond", "hawkish", "dovish", "quantitative",
    # growth and employment
    "jobs report", "payrolls", "unemployment", "jobless", "gdp", "recession",
    "soft landing", "stagflation", "deficit", "shutdown", "debt ceiling",
    "stimulus",
    # trade
    "tariff", "tariffs", "trade war", "sanctions", "embargo", "export ban",
    # ENERGY SUPPLY AND THE CHOKEPOINTS. Added 2026-09-13 after the filter
    # dropped a Strait of Hormuz vessel strike -- the single most macro story
    # on that tape. Crude moves QQQ through rates and margins, and a supply
    # headline rarely contains the word "oil": it names the waterway, the
    # tanker or the pipeline.
    "oil", "crude", "opec", "brent", "wti", "refinery", "pipeline", "tanker",
    "strait", "hormuz", "suez", "red sea", "barrel",
    # risk and currency
    "dollar", "vix", "volatility", "safe haven", "flight to quality",
}

# Oil-relevant states. A LOC/ORG entity here counts as macro CONTEXT, but only
# alongside a disruption word -- "Goldman picks China healthcare stocks" names
# a country and is not macro, while "strikes on Saudi" is.
MACRO_GEO = {
    "iran", "saudi", "saudi arabia", "russia", "ukraine", "venezuela",
    "opec", "middle east", "hormuz", "strait of hormuz", "red sea", "israel",
}
DISRUPTION = {
    "strike", "strikes", "struck", "attack", "attacked", "drone", "missile",
    "blockade", "blocked", "halt", "halted", "disruption", "shut", "seized",
    "war", "conflict", "restraint", "escalate", "escalation",
}

# NOT MACRO, whatever terms they happen to contain. Personal finance and
# promotional copy are the two families that slipped through on the 2026-09-13
# tape -- a freight-fund advert matched "crude, oil, tariffs" and scored +0.91,
# the second-biggest contributor to the macro mean, and a Social Security COLA
# column matched "inflation" at +0.51. Both pushed a bearish tape toward
# neutral. A reject here BEATS a macro match: these headlines do mention macro
# terms, which is exactly why a term list alone cannot exclude them.
REJECT_PATTERNS = [
    r"^\s*up \d",                      # "Up 3,600%, this freight fund..."
    r"\bthis (fund|etf|stock|trust|company|investment)\b",
    r"\b(sponsored|advertisement|promoted|paid post|partner content)\b",
    r"\d+%.{0,30}\b(fund|etf)\b",
    r"\byour\b", r"\bi'?m a\b", r"\bmy \w+ (is|are|was)\b",
    r"\bcola\b", r"\bretirement\b", r"\b401\(?k\)?\b", r"\bira\b",
    r"\bhow to\b", r"\bshould you\b", r"\bhere are the \d+\b",
    r"\bbest (stocks|etfs|funds|cds)\b", r"\bdividend stocks\b",
    r"\banalysts recommend\b", r"\bprice target\b",
]

# FINBERT RUNS AS AN API CALL, NOT A LOCAL MODEL. HF_TOKEN is already on the
# droplet and the router endpoint answers 200, so there is no torch, no
# transformers, and no 2GB of weights on a box with 2.6GB free. The classic
# api-inference.huggingface.co host no longer resolves; router.huggingface.co
# is the one that works.
FINBERT_URL = os.getenv(
    "TRADING_FINBERT_URL",
    "https://router.huggingface.co/hf-inference/models/ProsusAI/finbert")
FINBERT_MODEL = os.getenv("TRADING_FINBERT_MODEL", "ProsusAI/finbert")
HF_TOKEN = os.getenv("HF_TOKEN", "")
HF_BATCH = int(os.getenv("TRADING_HF_BATCH", "32"))

# NER IS AN API CALL TOO. Nothing is downloaded and nothing is hosted: no
# spaCy model, no torch, no weights on a box with 2.6GB free.
#
# THE TRADEOFF, STATED. spaCy is not an API service, so this is not spaCy --
# it is BERT-CoNLL03, which tags ORG/PER/LOC/MISC and NOT spaCy's MONEY,
# PERCENT or DATE. The filter here asks "does this story name a macro
# institution", which is an ORG/LOC question, so the missing types cost
# nothing today. If MONEY or DATE ever become part of the signal, that is the
# moment this has to go local.
NER_URL = os.getenv(
    "TRADING_NER_URL",
    "https://router.huggingface.co/hf-inference/models/"
    "dbmdz/bert-large-cased-finetuned-conll03-english")
MACRO_SYMBOL = os.getenv("TRADING_MACRO_SYMBOL", "QQQ")
LOOKBACK_HOURS = int(os.getenv("TRADING_MACRO_LOOKBACK_H", "24"))
OUT_PATH = os.getenv("TRADING_NEWS_ENRICHED_PATH", "/app/data/news_enriched.jsonl")

# How many macro headlines a verdict needs. One risk-off story is not a
# risk-off tape, and a mean over two swings on either of them.
MIN_ARTICLES = int(os.getenv("TRADING_MACRO_MIN_ARTICLES", "4"))

# How lopsided the vote must be to call a direction. 0.2 = 60/40.
VERDICT_MARGIN = float(os.getenv("TRADING_MACRO_MARGIN", "0.20"))

# FinBERT truncates at 512 tokens; title + summary is well inside that.
MAX_CHARS = int(os.getenv("TRADING_ENRICH_MAX_CHARS", "1200"))


_TERM_RX: dict = {}
_REJECT_RX = [re.compile(p, re.I) for p in REJECT_PATTERNS]


def has_term(term: str, text: str) -> bool:
    """Whole-word containment.

    NOT `term in text`. Substring matching put three personal-finance columns
    into the macro tape on the first dry run: "sec" matched Social SECurity and
    SECretary, "ppi" matched shiPPIng, "fed" matched FEDeral. Every false
    positive here is a vote in the macro mean, so this is not cosmetic.

    Compiled once per term and cached -- the same terms are tested against
    every article of every sweep.
    """
    rx = _TERM_RX.get(term)
    if rx is None:
        rx = _TERM_RX[term] = re.compile(r"\b" + re.escape(term) + r"\b")
    return bool(rx.search(text))




def _dsn() -> str:
    url = os.getenv("DATABASE_URL", "")
    return url.replace("postgresql+psycopg2://", "postgresql://").replace(
        "postgresql+asyncpg://", "postgresql://")


# --------------------------------------------------------------- models


def ner(texts: list) -> list:
    """Named entities via the HF Inference API. Returns [[{text, label}]].

    AN API, NOT A LOCAL MODEL. spaCy is not an API service, so "use the API"
    means an HF token-classification model instead of en_core_web_sm/_trf --
    the tradeoff being that this is BERT-CoNLL03, which tags ORG/PER/LOC/MISC
    and NOT spaCy's MONEY, PERCENT or DATE. For this pipeline that is the whole
    entity set that matters: the filter asks "does this story name a macro
    institution", which is an ORG/LOC question.

    Measured 2026-09-13, both endpoints answer 200. bert-large-cased-conll03
    over dslim/bert-base-NER because it returns whole entities -- base split
    FOMC into "F" + "##OMC", which no institution list will match.
    """
    import json
    import time
    import urllib.request

    if not HF_TOKEN:
        raise RuntimeError("HF_TOKEN is not set; NER needs it.")
    out = []
    for k in range(0, len(texts), HF_BATCH):
        chunk = texts[k:k + HF_BATCH]
        req = urllib.request.Request(
            NER_URL, data=json.dumps({"inputs": chunk}).encode(),
            headers={"Authorization": "Bearer " + HF_TOKEN,
                     "Content-Type": "application/json"})
        got = None
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=90) as resp:
                    got = json.loads(resp.read())
                break
            except Exception as exc:
                if attempt == 3:
                    raise
                wait = 5 * (attempt + 1)
                logger.warning("NER %s (%s) -- retrying in %ds",
                               type(exc).__name__, getattr(exc, "code", None),
                               wait)
                time.sleep(wait)
        # One input returns a flat list of entities; a batch returns one list
        # per input. Normalise so the caller always gets per-input lists.
        if got and isinstance(got[0], dict):
            got = [got]
        for ents in got:
            out.append([{"text": e.get("word", ""),
                         "label": e.get("entity_group") or e.get("entity", "")}
                        for e in ents])
    return out


def sentences(text: str) -> list:
    """Split on sentence enders. spaCy's doc.sents is gone with spaCy.

    Deliberately crude: this only picks WHICH sentences carry a macro term, and
    a split that occasionally keeps two sentences together costs a few extra
    words of context, not a wrong answer.
    """
    parts = re.split(r"(?<=[.!?])\s+", text)
    return [p.strip() for p in parts if p.strip()]


def finbert(texts: list) -> list:
    """Score texts through the HF Inference API. Returns [{label, score}].

    BATCHED, because one request per headline would exhaust the free tier in a
    single sweep. Retries on 503, which is what the endpoint returns while a
    cold model loads.

    THE RESPONSE SHAPE NEEDS UNWRAPPING. A batch of N comes back as a
    single-element list wrapping the N per-input results -- [[r1, r2, ... rN]]
    -- not as N lists. Measured 2026-09-13: 3 inputs returned len(out)==1 with
    3 dicts inside. Assuming one-list-per-input silently scores every headline
    with the first one's label.
    """
    import json
    import time
    import urllib.request

    if not HF_TOKEN:
        raise RuntimeError("HF_TOKEN is not set; FinBERT scoring needs it.")
    out = []
    for k in range(0, len(texts), HF_BATCH):
        chunk = texts[k:k + HF_BATCH]
        body = json.dumps({"inputs": chunk}).encode()
        req = urllib.request.Request(
            FINBERT_URL, data=body,
            headers={"Authorization": "Bearer " + HF_TOKEN,
                     "Content-Type": "application/json"})
        got = None
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    got = json.loads(resp.read())
                break
            except Exception as exc:
                code = getattr(exc, "code", None)
                if attempt == 3:
                    raise
                wait = 5 * (attempt + 1)
                logger.warning("FinBERT %s (%s) -- retrying in %ds",
                               type(exc).__name__, code, wait)
                time.sleep(wait)
        if len(got) == 1 and isinstance(got[0], list) and len(got[0]) == len(chunk):
            got = got[0]
        for r in got:
            d = r[0] if isinstance(r, list) else r
            out.append({"label": str(d["label"]).lower(),
                        "score": float(d["score"])})
    return out



# ============================================================================
# THE DIRECTIONAL LAYER: macro cause -> equity effect, written down.
#
# MEASURED ON TEN CONTROLLED HEADLINES whose direction for a long QQQ position
# is not arguable:
#
#     ProsusAI/finbert                           2/10   8 inverted
#     distilroberta-financial-news               4/10   5 inverted
#     these rules                               10/10
#
# The models are not noisy, they are MISMATCHED. FinBERT was trained on
# earnings sentences, where "revenue falls" is bad; macro inverts that, because
# "unemployment falls" is good and "oil spikes" is bad. That economics is not
# in the sentence, so no amount of prompt or threshold work recovers it and a
# different sentence classifier does not either -- distilroberta, trained on
# broader financial news, still inverted five.
#
# HONESTY ABOUT THE 10/10. These rules were written with those ten cases
# visible, and two of them (dropping "on" as a clause separator, adding "trade
# war") were changed to fix the last miss. Fitting to the test set is exactly
# what that is. 10/10 says the encoding is COHERENT, not that it generalises.
# The live-tape numbers below the fold are the only out-of-sample evidence.
#
# The rule itself is one line of economics: most macro drivers move INVERSELY
# to equities, a few move with them. Find the driver, find its direction, apply
# the sign.
INVERSE_DRIVERS = {
    "oil", "crude", "brent", "wti", "gas prices", "yield", "yields",
    "inflation", "cpi", "ppi", "pce", "rate", "rates", "rate hike",
    "tariff", "tariffs", "trade war", "unemployment", "jobless", "vix",
    "volatility", "recession", "deficit", "debt",
}
DIRECT_DRIVERS = {
    "stocks", "shares", "futures", "equities", "nasdaq", "s&p", "dow",
    "gdp", "payrolls", "jobs report", "growth", "earnings",
}
MOVE_UP = {
    "surge", "surges", "spike", "spikes", "jump", "jumps", "climb", "climbs",
    "rise", "rises", "soar", "soars", "raise", "raises", "raised", "hike",
    "hikes", "escalate", "escalates", "higher", "hot", "hotter", "top", "tops",
    "mount", "mounts", "boost", "boosts", "gain", "gains", "up", "rally",
    "blows past", "beat", "beats", "strong", "loom", "looms",
}
MOVE_DOWN = {
    "fall", "falls", "drop", "drops", "slide", "slides", "sink", "sinks",
    "ease", "eases", "cool", "cooler", "cut", "cuts", "lower", "retreat",
    "retreats", "roll back", "rolled back", "miss", "misses", "slip", "slips",
    "slump", "slumps", "tumble", "tumbles", "down", "weak", "steady", "hold",
    "holds",
}
# "on" is deliberately NOT a separator: it is a preposition far more often than
# a conjunction, and splitting on it cut "Tariffs on Chinese goods raised"
# between the driver and its direction.
CLAUSE_SPLIT = re.compile(r",|\bas\b|\bwhile\b|\bafter\b|\bamid\b|;|\.")


def macro_direction(text: str) -> tuple:
    """(+1 risk-on, -1 risk-off, 0 unknown, why) for a headline.

    CLAUSE BY CLAUSE, because a compound headline carries more than one fact.
    "Shares slip in Asia as oil climbs, rate hikes loom" is three, and scoring
    the whole string as one blurs them into whichever verb the model noticed.
    """
    votes, why = [], []
    for clause in CLAUSE_SPLIT.split(text):
        c = clause.strip()
        if len(c) < 4:
            continue
        up = any(has_term(w, c.lower()) for w in MOVE_UP)
        dn = any(has_term(w, c.lower()) for w in MOVE_DOWN)
        if up == dn:
            continue                       # no direction, or contradictory
        move = 1 if up else -1
        for d in INVERSE_DRIVERS:
            if has_term(d, c.lower()):
                votes.append(-move)
                why.append(d + ("+" if move > 0 else "-"))
                break
        else:
            for d in DIRECT_DRIVERS:
                if has_term(d, c.lower()):
                    votes.append(move)
                    why.append(d + ("+" if move > 0 else "-"))
                    break
    if not votes:
        # SUPPLY SHOCKS HAVE NO MOVEMENT VERB. "Vessel struck in Strait of
        # Hormuz" is unambiguously risk-off and contains no rise/fall at all --
        # it is an EVENT, and the price move is the consequence nobody has
        # written yet. On the 2026-09-13 tape the rules abstained on three such
        # headlines (the Hormuz strike, the Zaporizhzhia fuel attack, the
        # collapsed Hormuz talks), every one of them risk-off.
        #
        # Same economics as "oil up -> risk-off", one step earlier in the chain:
        # a disruption to oil geography raises crude, and crude raises the
        # discount rate on long-duration equity. One-sided on purpose -- an
        # attack is never risk-on, whereas "talks resume" is not reliably
        # risk-on either, so there is no symmetric rule to write.
        low = text.lower()
        geo = sorted({g for g in MACRO_GEO if has_term(g, low)})
        if geo and any(has_term(d, low) for d in DISRUPTION):
            return -1, "supply risk: " + ", ".join(geo[:3])
        return 0, "no driver+direction"
    total = sum(votes)
    return (1 if total > 0 else -1 if total < 0 else 0), " ".join(why)


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
    """NER FIRST, then FinBERT on what survives. Not in parallel.

    THE ORDER IS THE POINT, BUT NOT FOR THE REASON IT LOOKS LIKE. NER is the
    GATE here, not a scoper: it decides which stories are macro at all, and
    only those reach the scorer. Running the two in parallel
    scored everything and then threw most of it away -- wasted API calls, and
    worse, it invited the filter to be sloppy because nothing downstream
    depended on it. The first dry run scored "The future of retirement? Work
    until you die." and "Anthropic tells investors it will be profitable"
    (+0.89) as macro tape.

    SCOPING IS MEASURED AND IT IS CURRENTLY A NO-OP. Scoring the scoped text
    against the full text on 2026-09-13's tape: 0 of 11 labels differed, and
    the macro mean moved -0.206 -> -0.201, the same verdict. Macro RSS text is
    effectively SINGLE-SUBJECT, so the whole-text sentiment already is the
    subject's sentiment -- five of the eleven were multi-sentence and still
    scored identically. Nobody should treat the scoping below as load-bearing
    on the strength of it being there.

    It is kept because it costs nothing and stops being a no-op the moment the
    inputs get longer -- a full-body feed, or a summary covering two topics.
    Re-run the comparison before relying on it either way.

    WHAT SEQUENTIAL ACTUALLY BUYS is the call count: parallel scores all 47
    articles and discards 36, sequential scores the 11 that survive. Identical
    verdict, 4.3x the API calls on a free tier -- and this runs hourly with
    nothing waiting on it, so the latency parallelism buys is worth nothing.

    MULTI-SUBJECT TEXT IS THE TICKER LEG, and it never comes here. That is the
    case where NER-first genuinely changes the answer, and it is also the case
    FinBERT cannot do at all -- the Nike/SanDisk failure above. Polygon scores
    tickers per-ticker natively, which is why FinBERT never sees them.
    """
    texts = [extract_text(a) for a in articles]
    for a, t in zip(articles, texts):
        a["text"] = t

    # 1. NER gates. Entities kept on every record either way.
    for a, ents in zip(articles, ner(texts)):
        a["entities"] = ents
        a["is_macro"], a["macro_why"], a["scored_text"] = classify_macro(a, ents)

    # 2. DIRECTION FROM THE RULES, sentiment recorded beside it.
    #
    # The rule layer is the SIGNAL. FinBERT is still called and still stored,
    # because it costs one batched request and its rows are what will settle
    # whether it adds anything -- but it does not decide the verdict, because
    # measured against ten controlled macro headlines it was 2/10 with 8
    # inverted, and the inversions were on exactly the cases this book cares
    # about ("oil spikes", "yields surge" both read positive).
    macro = [a for a in articles if a["is_macro"]]
    if macro:
        for a in macro:
            a["rule_dir"], a["rule_why"] = macro_direction(a["scored_text"])
        try:
            for a, r in zip(macro, finbert([m["scored_text"] for m in macro])):
                a["sentiment"] = r["label"]
                a["sentiment_conf"] = r["score"]
                a["finbert_signed"] = (r["score"] if r["label"] == "positive"
                                       else -r["score"] if r["label"] == "negative"
                                       else 0.0)
        except Exception:
            # The verdict does not depend on it, so an outage is not fatal.
            logger.warning("FinBERT unavailable -- rules still decide.",
                           exc_info=True)
    for a in articles:
        a.setdefault("rule_dir", 0)
        a.setdefault("rule_why", "")
        a.setdefault("sentiment", None)
        a.setdefault("finbert_signed", None)
    return articles


def classify_macro(a: dict, ents: list) -> tuple:
    """(is_macro, why, text_to_score). Reject first, then three ways to qualify.

    ORDER MATTERS. REJECT_PATTERNS run BEFORE any macro test, because the
    headlines they catch genuinely do contain macro terms -- that is how they
    got in. A freight-fund advert saying "crude, oil, tariffs" cannot be
    excluded by tuning the term list; it has to be excluded by recognising the
    genre.

    Three ways to qualify, in order of confidence:
      1. an ORG/LOC entity that IS a macro institution (Fed, OPEC, BLS)
      2. a whole-word macro keyword (inflation, yields, hormuz)
      3. oil-relevant geography PLUS a disruption word -- "strikes on Saudi"
         qualifies, "China healthcare stocks" does not

    WORD BOUNDARIES THROUGHOUT. Substring matching put "sec" into Social
    SECurity and "ppi" into shiPPIng on the first run.
    """
    text = a.get("text") or a["title"]
    low = text.lower()

    for rx in _REJECT_RX:
        if rx.search(low):
            return False, "", text

    names = {e["text"].lower().strip() for e in ents
             if e["label"] in ("ORG", "LOC", "MISC", "GPE", "NORP")}
    hit_ent = sorted({m for m in MACRO_ORGS
                      for e in names
                      if e == m or has_term(m, e)})
    hit_kw = sorted({k for k in MACRO_KEYWORDS if has_term(k, low)})

    hit_geo = []
    geo = sorted({g for g in MACRO_GEO
                  if has_term(g, low) or any(has_term(g, e) for e in names)})
    if geo and any(has_term(d, low) for d in DISRUPTION):
        hit_geo = geo

    terms = set(hit_ent) | set(hit_kw) | set(hit_geo)
    if not terms:
        return False, "", text

    keep = [sent for sent in sentences(text)
            if any(has_term(t, sent.lower()) for t in terms)]
    scoped = " ".join(keep)[:MAX_CHARS] or a["title"]
    return True, ", ".join(sorted(terms)[:4]), scoped


# --------------------------------------------------------------- 5. output


def macro_verdict(scored: list) -> "tuple | None":
    """(label, score in [-1,1], n) from the rule votes, or None if too thin.

    NOT A MEAN OF SENTIMENT SCORES. A mean let one +0.91 advert cancel a real
    -0.95 risk-off headline on the 2026-09-13 tape and drag a bearish reading
    to neutral. Two defences:

      the votes are BOUNDED to +/-1, so no single article can carry the
      aggregate the way a 0.91 confidence could; and

      the score is the NET PROPORTION of directional votes, which is a
      majority measure -- it moves only when articles genuinely disagree in
      count, not when one of them is loud.

    Articles where the rules find no driver+direction do not vote. They are
    counted in n_seen and reported, because "twelve macro stories, two of them
    directional" is a materially different day from "twelve, all directional"
    and the verdict should not hide it.
    """
    votes = [a["rule_dir"] for a in scored if a.get("rule_dir")]
    if len(votes) < MIN_ARTICLES:
        logger.info("only %d directional macro article(s) of %d, below the %d "
                    "minimum -- no verdict", len(votes), len(scored),
                    MIN_ARTICLES)
        return None
    net = sum(votes) / len(votes)
    label = ("positive" if net > VERDICT_MARGIN
             else "negative" if net < -VERDICT_MARGIN else "neutral")
    return label, net, len(votes)


def store(verdict: tuple, now: datetime) -> int:
    """Write the macro read as source='finbert'.

    NOT 'polygon'. The first version reused that label so the existing reader
    would find the row without changing -- which would have made
    symbol_sentiment_hourly assert that Polygon produced a MACRO verdict, the
    one thing it structurally cannot produce (ticker=QQQ returns ETF
    comparisons). Any later question of the form "how accurate is the Polygon
    read" would have been silently answering it with FinBERT numbers for every
    QQQ row. A convenience in the writer is not worth a lie in the data.
    """
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
              now.date(), "finbert", label, mean, n,
              f"rule net {mean:+.2f} over {n} directional headline(s)"))
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
    for a in sorted(macro, key=lambda x: (x["rule_dir"], x["title"]))[:24]:
        d = {1: "RISK_ON ", -1: "RISK_OFF", 0: "  --    "}[a["rule_dir"]]
        fb = a.get("finbert_signed")
        print(f"  {d}  fb {('%+.2f' % fb) if fb is not None else '  -  '}  "
              f"[{a['rule_why'][:22]:22s}] {a['title'][:52]}")

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
