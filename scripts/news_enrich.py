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
    "war", "conflict", "escalate", "escalation",
}
# "restraint" WAS IN THE SET ABOVE AND IS ITS OPPOSITE. Caught 2026-09-13 by
# disagreeing with Gemini on "Iran and UAE back joint BRICS statement urging
# restraint": the rules called it RISK_OFF on supply risk, Gemini called it
# RISK_ON, and Gemini was right -- urging restraint is de-escalation, which
# should lower crude, not raise it. A hand-written keyword set gets exactly
# this wrong, and nothing in the pipeline would have caught it: the headline
# was macro, the geography matched, the vote looked reasonable, and it voted
# the wrong way. It took a second opinion that reasons about meaning.
DE_ESCALATION = {
    "restraint", "ceasefire", "truce", "de-escalate", "de-escalation",
    "peace", "talks resume", "agreement", "accord", "resolve",
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
# TOPICS, not headlines. Four wires carrying one story is one fact, and a
# verdict must not claim four. Two independent topics agreeing is a weaker
# claim than four but an honest one; below that there is no macro read.
MIN_TOPICS = int(os.getenv("TRADING_MACRO_MIN_TOPICS", "2"))

# How many agreeing topics a FULL-STRENGTH macro reading takes. Below this the
# score is scaled down, so VERY_BEARISH -- the only level that gates a call
# spread -- needs breadth and not just unanimity among a handful.
VERY_TOPICS = float(os.getenv("TRADING_MACRO_VERY_TOPICS", "5"))

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
# WHICH TOPIC A DRIVER BELONGS TO. Aggregation happens WITHIN a topic and then
# across topics, never across raw headlines -- see macro_verdict(). On the
# 2026-09-13 tape three of the seven directional headlines were the same Strait
# of Hormuz supply event reported three ways, so a flat count made one story
# worth three votes and would have swamped a single rates headline pointing the
# other way. Correlated rows are not independent evidence; the repo already
# learned that counting sessions rather than rows for correlated names.
TOPIC = {}
for _t, _ds in {
    # ENERGY INCLUDES THE GEOGRAPHY, not just the commodity. On the
    # 2026-09-13 tape three headlines about the Strait of Hormuz fell to
    # "other" because hormuz/strait/iran were not in this map, while the
    # oil-price headline went to "energy" -- so ONE macro driver counted as
    # TWO topics. Topic count sets the VERY_* threshold, so that split alone
    # took the reading from BEARISH (gates nothing) to VERY_BEARISH (refuses
    # every bullish entry on both books). A supply shock and the price move it
    # causes are one fact.
    "energy": {"oil", "crude", "brent", "wti", "gas prices", "supply",
               "opec", "refinery", "pipeline", "tanker", "barrel",
               "hormuz", "strait", "strait of hormuz", "suez", "red sea",
               "iran", "saudi", "saudi arabia", "russia", "ukraine",
               "venezuela", "middle east", "israel"},
    "rates": {"yield", "yields", "rate", "rates", "rate hike"},
    "inflation": {"inflation", "cpi", "ppi", "pce"},
    "trade": {"tariff", "tariffs", "trade war"},
    "labour": {"unemployment", "jobless", "payrolls", "jobs report"},
    "growth": {"gdp", "growth", "recession", "deficit", "debt", "earnings"},
    "risk": {"vix", "volatility"},
    "equities": {"stocks", "shares", "futures", "equities", "nasdaq", "s&p",
                 "dow"},
}.items():
    for _d in _ds:
        TOPIC[_d] = _t

# "on" is deliberately NOT a separator: it is a preposition far more often than
# a conjunction, and splitting on it cut "Tariffs on Chinese goods raised"
# between the driver and its direction.
CLAUSE_SPLIT = re.compile(r",|\bas\b|\bwhile\b|\bafter\b|\bamid\b|;|\.")


# AN INSTRUCT LLM, WHEN ONE IS AFFORDABLE. Measured 2026-09-13 on the same ten
# controlled headlines:
#
#     ProsusAI/finbert               2/10   8 inverted
#     distilroberta-financial-news   4/10   5 inverted
#     Llama-3.1-8B-Instruct          8/8    0 inverted, then HTTP 402
#     these rules                   10/10   but FITTED to that set
#
# LLAMA'S 8/8 IS THE STRONGER RESULT, and the comparison should say so. Nothing
# was tuned to it -- one prompt, first attempt -- whereas the rules were written
# with those ten cases in view and two were changed to fix the last miss. An
# unfitted 8/8 is better evidence than a fitted 10/10.
#
# It is not the default because it CANNOT RUN: the HF account is on the free
# plan with canPay=false, and /v1/chat/completions routes to paid providers, so
# call nine returned 402 Payment Required. FinBERT and the NER model are
# unaffected -- those sit on hf-inference, which has its own allowance.
#
# So: rules decide, and the LLM is consulted ONLY where the rules abstain --
# roughly five headlines an hour rather than twelve, which is the cheap half of
# the problem anyway. Any failure, 402 included, falls back silently to the
# rules' answer. Set TRADING_MACRO_LLM=true once billing exists.
MACRO_LLM = os.getenv("TRADING_MACRO_LLM", "false").lower() == "true"

# WHICH PROVIDER ANSWERS. "gemini" or "hf".
#
# gemini  gemini-2.5-flash on the REST endpoint. Its free tier is real -- the
#         per-day allowance is far above the ~35 abstentions this makes -- so
#         unlike the HF route it does not need a payment method. REST rather
#         than the google-genai SDK ON PURPOSE: ~900MB of torch/spaCy was just
#         removed from this box to keep the models hosted, and adding an SDK to
#         call a hosted model would walk that back for no gain. One POST, no
#         dependency.
#
# hf      meta-llama/Llama-3.1-8B-Instruct via router.huggingface.co. Measured
#         8/8 with 0 inversions, then HTTP 402: the account is free with
#         canPay=false and /v1/chat/completions routes to PAID providers. Worse
#         than simply unavailable -- two runs of the same tape gave 4 topics
#         then 3, because one call landed before the quota bit. A verdict that
#         depends on whether credits happened to be free that hour is not a
#         gate anyone can reason about.
MACRO_LLM_PROVIDER = os.getenv("TRADING_MACRO_LLM_PROVIDER", "gemini").lower()
GEMINI_KEY = os.getenv("GEMINI_API_KEY", "")
# gemini-2.5-flash IS LISTED BY THE MODELS ENDPOINT AND STILL 404s: "no longer
# available to new users". The listing is not the availability check -- the only
# reliable test is a generateContent call with the actual key. Probed
# 2026-09-13: 3.6-flash, flash-latest and 3.5-flash all failed too;
# 3.1-flash-lite answered.
GEMINI_MODEL = os.getenv("TRADING_GEMINI_MODEL", "gemini-3.1-flash-lite")
GEMINI_URL = ("https://generativelanguage.googleapis.com/v1beta/models/"
              "{model}:generateContent")
MACRO_LLM_URL = os.getenv("TRADING_MACRO_LLM_URL",
                          "https://router.huggingface.co/v1/chat/completions")
MACRO_LLM_MODEL = os.getenv("TRADING_MACRO_LLM_MODEL",
                            "meta-llama/Llama-3.1-8B-Instruct")
_LLM_SYSTEM = (
    "You classify the likely SAME-DAY impact of a news headline on a LONG "
    "position in US large-cap equities (QQQ). Answer with exactly one token: "
    "RISK_ON, RISK_OFF, or NEUTRAL. RISK_OFF means equities likely fall. "
    "Higher oil, higher yields, higher inflation, rate hikes, tariffs and "
    "supply disruptions are RISK_OFF; their opposites are RISK_ON. A headline "
    "with no market-moving content is NEUTRAL."
)
_LLM_DEAD = False


def llm_direction(headline: str) -> tuple:
    """(+1/-1/0, why) from the instruct model, or (0, reason) on any failure.

    ONE STRIKE AND IT STOPS FOR THE RUN. A 402 is not transient -- the credits
    are gone until the month turns -- so retrying it once per abstained
    headline would burn the whole sweep on identical failures. _LLM_DEAD
    latches.
    """
    global _LLM_DEAD
    if _LLM_DEAD:
        return 0, "llm off"
    import json
    import urllib.request

    if MACRO_LLM_PROVIDER == "gemini":
        if not GEMINI_KEY:
            _LLM_DEAD = True
            logger.warning("GEMINI_API_KEY is not set -- rules only.")
            return 0, "no gemini key"
        try:
            req = urllib.request.Request(
                GEMINI_URL.format(model=GEMINI_MODEL) + "?key=" + GEMINI_KEY,
                data=json.dumps({
                    "systemInstruction": {"parts": [{"text": _LLM_SYSTEM}]},
                    "contents": [{"parts": [{"text": headline}]}],
                    # Deterministic, and short: one token is the whole answer,
                    # so anything longer is the model explaining itself into a
                    # regex that will ignore it.
                    "generationConfig": {"temperature": 0.0,
                                         "maxOutputTokens": 8},
                }).encode(),
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=45) as r:
                out = json.loads(r.read())
            txt = out["candidates"][0]["content"]["parts"][0]["text"]
        except Exception as exc:
            code = getattr(exc, "code", None)
            _LLM_DEAD = True
            logger.warning("Gemini unavailable (%s) -- rules only for the rest "
                           "of this run.", code)
            return 0, f"gemini error {code}"
        m = re.search(r"RISK_ON|RISK_OFF|NEUTRAL", txt.upper())
        if not m:
            return 0, "gemini unparsed"
        tok = m.group(0)
        return (1 if tok == "RISK_ON"
                else -1 if tok == "RISK_OFF" else 0), "gemini"

    if not HF_TOKEN:
        return 0, "llm off"
    try:
        req = urllib.request.Request(
            MACRO_LLM_URL,
            data=json.dumps({
                "model": MACRO_LLM_MODEL,
                "messages": [{"role": "system", "content": _LLM_SYSTEM},
                             {"role": "user", "content": headline}],
                "max_tokens": 8, "temperature": 0.0,
            }).encode(),
            headers={"Authorization": "Bearer " + HF_TOKEN,
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=45) as r:
            txt = json.loads(r.read())["choices"][0]["message"]["content"]
    except Exception as exc:
        code = getattr(exc, "code", None)
        _LLM_DEAD = True
        logger.warning("Macro LLM unavailable (%s) -- rules only for the rest "
                       "of this run.%s", code,
                       "  402 means the HF free credits are spent."
                       if code == 402 else "")
        return 0, f"llm error {code}"
    m = re.search(r"RISK_ON|RISK_OFF|NEUTRAL", txt.upper())
    if not m:
        return 0, "llm unparsed"
    tok = m.group(0)
    return (1 if tok == "RISK_ON" else -1 if tok == "RISK_OFF" else 0), "llm"


def macro_direction(text: str) -> tuple:
    """(+1 risk-on, -1 risk-off, 0 unknown, why) for a headline.

    CLAUSE BY CLAUSE, because a compound headline carries more than one fact.
    "Shares slip in Asia as oil climbs, rate hikes loom" is three, and scoring
    the whole string as one blurs them into whichever verb the model noticed.
    """
    votes, why, drivers = [], [], []
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
                drivers.append(d)
                break
        else:
            for d in DIRECT_DRIVERS:
                if has_term(d, c.lower()):
                    votes.append(move)
                    why.append(d + ("+" if move > 0 else "-"))
                    drivers.append(d)
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
            # De-escalation beats disruption: "strikes halted after ceasefire"
            # is not a supply shock. Abstain rather than guess the sign -- the
            # LLM sees these next and is better at them.
            if any(has_term(d, low) for d in DE_ESCALATION):
                return 0, "geo, but de-escalatory", None
            return -1, "supply risk: " + ", ".join(geo[:3]), "energy"
        return 0, "no driver+direction", None
    total = sum(votes)
    # ONE TOPIC PER HEADLINE -- the first driver found. A headline counted in
    # two topics votes twice, which is the double-count this exists to stop.
    topic = next((TOPIC[d] for d in drivers if d in TOPIC), "other")
    return (1 if total > 0 else -1 if total < 0 else 0), " ".join(why), topic


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


# ------------------------------------------- 2b. promo filter (no model)


def drop_promos(articles: list) -> tuple:
    """Reject sponsored and personal-finance copy BEFORE any model sees it.

    ORDER MATTERS AND THIS RUNS FIRST. It used to sit inside classify_macro,
    which fires AFTER the NER request -- so every advert was paying for a model
    round trip before being thrown away. A regex pass costs nothing and shrinks
    the NER payload by whatever it removes.

    IT CANNOT BE DONE WITH A TERM LIST, which is the point. These headlines
    genuinely contain macro terms -- that is how they got in. On the
    2026-09-13 tape a freight-fund advert matched "crude, oil, tariffs" and
    scored +0.91, the second-largest contributor to the macro mean, and a
    Social Security COLA column matched "inflation" at +0.51. Both pushed a
    bearish tape toward neutral. The genre has to be recognised, not the terms.
    """
    keep, dropped = [], []
    for a in articles:
        blob = (a["title"] + " " + (a.get("summary") or "")).lower()
        if any(rx.search(blob) for rx in _REJECT_RX):
            dropped.append(a)
        else:
            keep.append(a)
    if dropped:
        logger.info("promo filter: dropped %d before any model call", len(dropped))
    return keep, dropped


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
            (a["rule_dir"], a["rule_why"],
             a["topic"]) = macro_direction(a["scored_text"])
            # ONLY WHERE THE RULES ABSTAIN. The rules are free and were right
            # on every call they made today; the LLM is for the gap, not a
            # second opinion on answers that already exist.
            if MACRO_LLM and not a["rule_dir"]:
                d, why = llm_direction(a["title"])
                if d:
                    a["rule_dir"], a["rule_why"] = d, why
                    # TOPIC FROM THE TERMS THE FILTER ALREADY MATCHED, not
                    # "other". Dumping every LLM answer into one bucket puts
                    # unrelated headlines in the same vote, where they cancel:
                    # on the 2026-09-13 tape a gas-prices/rates piece and a
                    # Treasury-yields piece both landed in "other" and silenced
                    # each other, despite both being about rates and both
                    # having been answered. macro_why holds the matched terms;
                    # the first one that maps wins, same rule the driver path
                    # uses.
                    a["topic"] = next(
                        (TOPIC[t] for t in
                         (x.strip() for x in (a.get("macro_why") or "").split(","))
                         if t in TOPIC), "other")
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
        a.setdefault("topic", None)
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
    # No reject pass here: drop_promos() ran before the NER call and owns it.
    text = a.get("text") or a["title"]
    low = text.lower()
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


# WHY CLUSTERING HAPPENS HERE AND NOT BEFORE THE SCORERS.
#
# Clustering earlier would let the rules and Gemini resolve a cluster ONCE
# instead of per headline, which saves API calls when coverage is heavily
# duplicated. Measured on the 2026-09-13 tape rather than assumed:
#
#     10 macro headlines -> 10 distinct events, 0 carried by more than one wire
#     5 resolved by rules (free), 5 abstentions (the only paid calls)
#
# THE FREE LAYER ALREADY ABSORBS THE DUPLICATION, which is the structural
# reason this ordering holds. Syndicated stories are the big obvious ones -- a
# Hormuz strike, an oil move, a Fed decision -- and those are exactly the ones
# the rules answer for nothing. What reaches Gemini is the residue: analytical
# and opinion pieces, which are distinct by nature and do not duplicate. So the
# cost that clustering would reduce is the cost that is already zero.
#
# AND THE NAIVE VERSION IS ACTIVELY WRONG. Abstentions carry no topic -- a
# topic comes from a matched driver, and abstaining means none matched -- so
# clustering them by topic drops all five into "other" and resolves five
# unrelated stories with one call and one answer. The measurement script
# reported that as "4 calls saved"; it is four answers destroyed.
#
# Re-measure on a weekday before revisiting: Sunday is a low-syndication tape
# and this is a lower bound on duplicate coverage.
def save_scores(articles: list) -> int:
    """Write each scored article's direction back onto its news_seen row.

    SCORING IS ONCE PER ARTICLE; COUNTING IS PER SWEEP. Dedupe correctly stops
    an article being scored twice -- it is the same article. But it was also
    stopping it being COUNTED twice, and those are different questions. The
    09:25 sweep sees a full 24h and votes on a dozen topics; the 11:25 sweep
    sees only what published in the last hour, falls below MIN_TOPICS, writes
    nothing, and the gates go on reading the 09:25 row. The hourly re-grade
    collapses to once-a-day and looks like a quiet tape rather than a closed
    window.
    """
    rows = [(a.get("rule_dir"), a.get("topic"), a.get("published"), a["guid"])
            for a in articles if a.get("rule_dir")]
    if not rows:
        return 0
    try:
        conn = psycopg2.connect(_dsn())
        conn.autocommit = True
        with conn, conn.cursor() as cur:
            cur.executemany(
                "UPDATE news_seen SET rule_dir=%s, topic=%s, published=%s, "
                "scored_at=now() WHERE guid=%s", rows)
        conn.close()
        return len(rows)
    except Exception:
        logger.warning("Could not persist scores -- this sweep's verdict still "
                       "stands, the next one loses this hour.", exc_info=True)
        return 0


def window_scores() -> list:
    """Every article scored inside the lookback window, as {rule_dir, topic}.

    This is what the verdict aggregates, so an hour with two new headlines
    still produces a verdict from the whole window rather than from two.
    """
    try:
        conn = psycopg2.connect(_dsn())
        with conn, conn.cursor() as cur:
            cur.execute(
                "SELECT rule_dir, topic FROM news_seen "
                "WHERE rule_dir IS NOT NULL AND published IS NOT NULL "
                "  AND published >= now() - (%s || ' hours')::interval",
                (LOOKBACK_HOURS,))
            rows = cur.fetchall()
        conn.close()
        return [{"rule_dir": d, "topic": t} for d, t in rows]
    except Exception:
        logger.warning("Could not read the window -- falling back to this "
                       "sweep only.", exc_info=True)
        return []


def macro_verdict(scored: list) -> "tuple | None":
    """(label, net in [-1,1], n_topics, per_topic) or None if too thin.

    TWO STAGES, AND NEVER ONE BLEND. Headlines aggregate WITHIN a topic, and
    topics aggregate across. Nothing averages an oil headline against a
    payrolls headline as though they were two draws from one distribution --
    they are two different facts about two different things.

    WHY, CONCRETELY. On the 2026-09-13 tape three of the seven directional
    headlines were the SAME Strait of Hormuz supply event, reported by three
    wires. A flat count made one story worth three votes, so it would have
    outvoted a rates headline pointing the other way purely by being
    syndicated. Correlated rows are not independent evidence. This repository
    already learned that lesson once, clustering by session rather than
    counting rows for correlated names.

    So each topic gets ONE vote: the sign of its internal majority, regardless
    of how many wires carried it. The overall score is the net across topic
    votes, which is bounded, resistant to a single loud article, and resistant
    to a single heavily-syndicated story -- the failure a mean of confidences
    could not survive at all.

    MIN_ARTICLES now counts TOPICS, not headlines. Four wires on one story is
    one fact, and a verdict should not claim four.
    """
    directional = [a for a in scored if a.get("rule_dir")]
    by_topic: dict = {}
    for a in directional:
        by_topic.setdefault(a.get("topic") or "other", []).append(a["rule_dir"])

    per_topic = {}
    for t, votes in by_topic.items():
        net = sum(votes)
        per_topic[t] = (1 if net > 0 else -1 if net < 0 else 0, len(votes))

    # "other" DOES NOT VOTE. It is the bucket for headlines whose terms map to
    # no named topic, so its members are unrelated by construction -- summing
    # them produces a "topic" that is really an average of leftovers, and it
    # counts toward the topic total that sets the VERY_* threshold. Recorded
    # and shown, never counted. If it is ever large, that is a signal the topic
    # map has a gap, not that the tape has a fifth theme.
    voting = [d for t, (d, _) in per_topic.items() if d and t != "other"]
    if len(voting) < MIN_TOPICS:
        logger.info("only %d directional topic(s) from %d headline(s), below "
                    "the %d minimum -- no verdict", len(voting),
                    len(directional), MIN_TOPICS)
        return None
    net = sum(voting) / len(voting)
    label = ("positive" if net > VERDICT_MARGIN
             else "negative" if net < -VERDICT_MARGIN else "neutral")
    return label, net, len(voting), per_topic


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
    label, net, n = verdict[0], verdict[1], verdict[2]
    # SCALE AGREEMENT BY EVIDENCE BEFORE STORING.
    #
    # net is the PROPORTION of topics agreeing, so three topics leaning one way
    # gives -1.0 -- identical to thirty. Downstream, _verdict_from_score() maps
    # |score| >= 0.70 to VERY_*, and VERY_BEARISH at confidence 1.00 refuses
    # every call debit spread on both books. On the 2026-09-13 tape that is
    # exactly what three unanimous topics would have done: a whole day of
    # bullish entries refused on the strength of three agreeing wires.
    #
    # That is the August macro gate, which refused 55 of 55 cycles on a day QQQ
    # rose $6.50 off its low. Unanimity is CHEAP when there are few topics.
    #
    # So the stored score is net weighted by how much evidence produced it:
    # VERY_* now needs the tape to agree AND to have said enough to be worth
    # believing. Three unanimous topics store -0.60 (BEARISH, conf 0.60, below
    # the 0.70 direction-gate floor -- recorded, gating nothing). Five store
    # -1.00, which is the genuinely rare day the tail guard exists for.
    scaled = net * min(1.0, n / VERY_TOPICS)
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
              now.date(), "finbert", label, scaled, n,
              f"net {net:+.2f} over {n} topic(s), scaled {scaled:+.2f}"))
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
    # BEFORE ANY MODEL CALL. Cheap, and it shrinks the NER payload too.
    arts, promos = drop_promos(arts)
    print(f"\n{len(arts)} new article(s) inside {LOOKBACK_HOURS}h "
          f"({len(promos)} sponsored/personal-finance dropped before any model)")
    for a in promos[:6]:
        print(f"    promo: {a['title'][:66]}")
    # NO EARLY RETURN ON AN EMPTY SWEEP. Nothing new to SCORE is not nothing to
    # SAY: the window still holds the day's macro picture, and a quiet hour
    # must re-affirm the verdict rather than leave the gates on a row that ages
    # silently. This returned here until 2026-09-13, which meant the second
    # sweep of any hour produced no verdict at all.
    if not arts:
        print("nothing new to score -- verdict still recomputed from the window")

    arts = enrich(arts) if arts else []
    macro = [a for a in arts if a["is_macro"]]
    print(f"{len(macro)} macro after the NER filter "
          f"({len(arts) - len(macro)} dropped as single-name or off-topic)\n")
    for a in sorted(macro, key=lambda x: (x["rule_dir"], x["title"]))[:24]:
        d = {1: "RISK_ON ", -1: "RISK_OFF", 0: "  --    "}[a["rule_dir"]]
        fb = a.get("finbert_signed")
        print(f"  {d}  fb {('%+.2f' % fb) if fb is not None else '  -  '}  "
              f"[{a['rule_why'][:22]:22s}] {a['title'][:52]}")

    saved = save_scores(arts)
    # THE WINDOW, NOT THIS SWEEP. window_scores() returns every article scored
    # in the last LOOKBACK_HOURS, so a quiet hour still votes on the day's
    # accumulated macro picture. Falls back to this sweep's articles if the
    # store is unreachable, which is strictly worse but never nothing.
    window = window_scores() or macro
    print(f"\nscored {saved} new; verdict over {len(window)} article(s) "
          f"in the last {LOOKBACK_HOURS}h")
    v = macro_verdict(window)
    if not v:
        print("\nNo macro verdict -- too few macro headlines. NOTHING STORED, "
              "which leaves the gates on the last good verdict rather than a "
              "manufactured neutral.")
        return
    label, net, n, per_topic = v
    print("\nBY TOPIC (one vote each, however many wires carried it):")
    for t, (d, cnt) in sorted(per_topic.items()):
        arrow = {1: "RISK_ON ", -1: "RISK_OFF", 0: "  split "}[d]
        note = "   (not counted -- unmapped terms)" if t == "other" else ""
        print(f"  {t:<10} {arrow}  from {cnt} headline(s){note}")
    print(f"\nMACRO VERDICT  {label.upper()}  net {net:+.3f}  over {n} topic(s)")
    if args.dry_run:
        print("DRY RUN — nothing written.")
        return
    print(f"wrote {store(v, now)} row(s); "
          f"{write_jsonl(arts)} record(s) -> {OUT_PATH}")


if __name__ == "__main__":
    main()
