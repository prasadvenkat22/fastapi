"""Macro news: RSS in, one Gemini call, one macro verdict out.

    RSS -> feedparser -> GUID dedupe -> promo regex -> Gemini -> topic
           clustering -> QQQ macro row

WHY THIS EXISTS. Polygon supplies per-ticker sentiment and structurally cannot
supply a macro read -- measured 2026-09-12, `ticker=QQQ` returns ETF
comparisons ("Forget JEPQ...") and market-wide news matched 0 of the 114 macro
terms then in use. So the macro tape comes from general wires and something has
to score it.

ONE MODEL, NOT THREE STAGES (2026-09-14). This ran NER (which stories are
macro) -> FinBERT (sentiment) -> hand-written rules (direction), and every
stage had its own failure mode, its own tuning surface and its own bugs. Gemini
does all three in one call and the code is a third the size.

WHAT EACH STAGE COST BEFORE IT WENT, because the replacements have to be at
least this good and the record is the only way to check:

    NER      decided macro/skip from a keyword list. "sec" matched Social
             SECurity, "ppi" matched shiPPIng, and a Strait of Hormuz vessel
             strike -- the most macro story on that tape -- was DROPPED for
             saying neither "oil" nor any listed institution.

    FinBERT  scored sentiment and was measured at 2/10 on controlled macro
             headlines with 8 INVERTED. It reads word polarity, not economic
             implication: "unemployment falls" is a fall, "oil spikes" is a
             spike. distilroberta, trained on broader financial news, still
             inverted 5. This is a training-domain mismatch, not noise, and no
             sentence classifier fixes it. FinBERT was already decided-nothing
             by the time it was removed.

    rules    encoded the causality FinBERT lacked and scored 10/10 -- but on
             the same ten cases they were written against, with two rules
             changed to fix the last miss. A fitted 10/10.

    gemini   scored 9/10 UNFITTED on those ten, and caught a rules bug nothing
             else could: "restraint" had been filed in the DISRUPTION set, its
             own antonym, so a de-escalation headline voted risk-off.

WHAT IS GIVEN UP, PLAINLY. The rules were free, deterministic and auditable --
"oil+ rate+" said exactly why. This is a paid API at temperature 0, and when it
is unreachable there is no macro verdict at all rather than a degraded one. The
gates read the last stored verdict or nothing, which is the safe direction, but
it is a real loss of independence. TRADING_MACRO_MIN_TOPICS still governs
whether a thin read becomes a verdict.

NOTHING HERE TRADES. It writes one QQQ row per sweep; news_watch.py turns that
into the verdict the gates read.

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

# COMPANY FEEDS: STORED FOR THE TICKER GRADER, NEVER SENT TO THE MACRO MODEL.
#
# Added 2026-09-19 after Friday's miss. Polygon held ONE SanDisk article all
# day (a Zacks roundup); the Cramer flag on $90M of short-dated call blocks in
# Sandisk, Micron and Marvell (Benzinga, 10:39 ET) reached none of the feeds
# above, and the S&P 100 inclusion that produced the closing spike had been
# announced two weeks earlier on a PR Newswire release this sweep never read.
# The ticker leg in news_hourly matches news_seen titles on symbol aliases, so
# a headline stored here is graded for its name on the next hourly run.
#
# These are single-name wires and filings. They go into news_seen like every
# other row and are NOT classified for the macro verdict: 400 company
# headlines an hour would swamp a macro read that counts one vote per topic,
# and would cost a Gemini call each for nothing the macro gate can use.
#
#   benzinga       the whole Benzinga wire, the one feed that carried Friday
#   gnews-*        Google News RSS search, which is the only public feed that
#                  reaches S&P Dow Jones Indices releases (spglobal.com and
#                  its RSS 403 every non-browser client) and Business Wire
#                  corporate releases by name. Titles arrive "Headline - Source".
#   edgar-*        SEC EDGAR Atom per company, 8-K and SC 13D, by numeric CIK
#                  (the ticker form of the URL returned nothing for SNDK).
#                  EDGAR entries are titled "8-K - Current report" with no
#                  company name, so each feed carries a PREFIX that puts the
#                  filer's name in the title -- without it the alias matcher
#                  could never attribute the filing. The SEC requires a
#                  User-Agent with a contact address and answers 403 without
#                  one: TRADING_SEC_USER_AGENT.
_UNIVERSE = "Sandisk OR Micron OR Marvell OR Broadcom OR Intel OR Nvidia OR CoreWeave OR Seagate OR \"Western Digital\" OR Dell OR \"Palo Alto\" OR Tesla OR Amazon OR Apple OR Meta OR Microsoft OR Alphabet OR AMD"
_GN = "https://news.google.com/rss/search?hl=en-US&gl=US&ceid=US:en&q="
SEC_USER_AGENT = os.getenv("TRADING_SEC_USER_AGENT", "").strip()
# ticker -> (CIK, filer name) from https://www.sec.gov/files/company_tickers.json, 2026-09-19
EDGAR_CIK = {
    "SNDK": (2023554, "Sandisk Corp"), "MU": (723125, "Micron Technology"),
    "NVDA": (1045810, "Nvidia Corp"), "TSLA": (1318605, "Tesla Inc"),
    "AMZN": (1018724, "Amazon.com Inc"), "AAPL": (320193, "Apple Inc"),
    "META": (1326801, "Meta Platforms"), "MSFT": (789019, "Microsoft Corp"),
    "GOOGL": (1652044, "Alphabet Inc"), "AMD": (2488, "Advanced Micro Devices"),
    "AVGO": (1730168, "Broadcom Inc"), "INTC": (50863, "Intel Corp"),
    "CRWV": (1769628, "CoreWeave Inc"), "MRVL": (1835632, "Marvell Technology"),
    "PANW": (1327567, "Palo Alto Networks"), "DELL": (1571996, "Dell Technologies"),
    "STX": (1137789, "Seagate Technology"), "WDC": (106040, "Western Digital Corp"),
}
COMPANY_FEEDS = [
    {"name": "benzinga", "url": "https://www.benzinga.com/feed"},
    {"name": "gnews-index", "url": _GN + (
        "(%22S%26P+500%22+OR+%22S%26P+100%22+OR+%22Nasdaq-100%22)+"
        "(%22set+to+join%22+OR+%22to+join+the%22+OR+%22will+replace%22+OR+"
        "%22added+to+the%22+OR+%22rebalance%22)")},
    {"name": "gnews-spdji", "url": _GN + "site:prnewswire.com+%22S%26P+Dow+Jones+Indices%22"},
    {"name": "gnews-businesswire", "url": _GN + "site:businesswire.com+(" + _UNIVERSE.replace(" ", "+") + ")"},
    {"name": "gnews-benzinga", "url": _GN + "site:benzinga.com+(" + _UNIVERSE.replace(" ", "+") + ")"},
    {"name": "gnews-benzinga-options", "url": _GN + (
        "site:benzinga.com+(%22option%22+OR+%22options%22+OR+%22calls%22+OR+%22puts%22)+("
        + _UNIVERSE.replace(" ", "+") + ")")},
] + [
    {"name": f"edgar-{t.lower()}-{form.lower().replace(' ', '')}",
     "url": ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
             f"&CIK={cik}&type={form.replace(' ', '%20')}&output=atom&count=10"),
     "prefix": f"{filer} {form} filing: ", "agent": SEC_USER_AGENT}
    for t, (cik, filer) in EDGAR_CIK.items() for form in ("8-K", "SC 13D")
]

# "This story is about the economy, not a company." ORG alone cannot decide it
# -- every company story has an ORG and most carry MONEY and PERCENT, so a
# type-based rule keeps everything, which is the failure this filter exists to
# prevent. A story qualifies on a macro INSTITUTION or a macro KEYWORD.
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
# Compiled once. This lived beside the rules engine's regex cache until
# 2026-09-14 and went out with it -- caught by the first dry run, which is what
# dry runs are for.
_REJECT_RX = [re.compile(p, re.I) for p in REJECT_PATTERNS]

# HF_TOKEN, FINBERT_URL and NER_URL were here and are gone (2026-09-14).
# Nothing in this file calls HuggingFace any more -- Gemini does all three jobs
# those two models were doing. The HF account can be left alone or the token
# revoked; nothing here reads it.
MACRO_SYMBOL = os.getenv("TRADING_MACRO_SYMBOL", "QQQ")
# TWO WINDOWS, NOT ONE, AND THE SECOND ONE IS WHY.
#
# FETCH is wide: 24h, so a story that broke overnight is still picked up at the
# open. Dedupe means a re-seen article costs nothing.
#
# THE VERDICT WINDOW USED TO BE THE SAME 24 HOURS, AND THAT MADE THE READ
# UNABLE TO MOVE. Measured 2026-09-14: the QQQ macro verdict printed -0.67 at
# 09:00, 10:00, 11:00, 12:00, 13:00, 14:00 and 15:00 ET -- seven identical
# readings -- while crude fell 3%, the 10Y turned from +2.9bp to -1.8bp, VIX
# collapsed 4% and QQQ rose 1.13% off an 11:00 turn. One new hour of headlines
# against twenty-three hours of yesterday's cannot shift a topic vote, so the
# verdict was frozen by construction.
#
# THE CONSEQUENCE WAS WORSE THAN A STALE NUMBER: the turn gate added on
# 2026-09-13 watches for a CHANGE from the opening verdict, and with a 24h
# window there is no change to see. It was a gate whose input could not produce
# the event it was built to catch.
#
# 4 hours: long enough that a quiet hour still votes on something, short enough
# that a real turn shows up within one or two sweeps. At 09:12 it reaches back
# to 05:12, which covers the pre-market tape where most overnight macro lands.
LOOKBACK_HOURS = int(os.getenv("TRADING_MACRO_LOOKBACK_H", "24"))
VERDICT_WINDOW_H = int(os.getenv("TRADING_MACRO_VERDICT_WINDOW_H", "4"))
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


def _dsn() -> str:
    url = os.getenv("DATABASE_URL", "")
    return url.replace("postgresql+psycopg2://", "postgresql://").replace(
        "postgresql+asyncpg://", "postgresql://")


# --------------------------------------------------------------- models


# ============================================================================
# ONE CALL: macro/skip, topic, and direction together.
#
# The prompt carries the two corrections the staged pipeline had to learn the
# hard way, because a fresh model makes both mistakes:
#
#   PRICE ROUNDUPS ARE NOT MACRO. Measured 2026-09-14: Gemini classified "Gold
#   prices today", "Silver prices today" and "Bitcoin and ethereum prices
#   today" as macro, which put 10 headlines into one "risk" bucket and dragged
#   the verdict to NEGATIVE on its own. A daily price table is not an event.
#
#   DIRECTION IS ECONOMIC, NOT LEXICAL. Spelled out in the prompt because it is
#   exactly what FinBERT could not do: higher oil, higher yields, higher
#   inflation, rate hikes, tariffs and supply disruptions are risk-OFF for a
#   long equity position, however positive the words look.
GEMINI_SYSTEM = """You triage financial news headlines for an options trading \
system that holds LONG positions in US large-cap equities (QQQ).

For EACH numbered headline return one object:
  "i"      the headline's number
  "macro"  true ONLY if it is about the ECONOMY or MARKET-WIDE conditions:
           central banks, rates, inflation, jobs, GDP, trade policy, energy
           supply, or geopolitics that moves oil.
           false for: single-company news, analyst picks, personal finance,
           retirement or savings advice, fund promotions, and DAILY PRICE
           ROUNDUPS ("Gold prices today", "Best CD rates today", crypto price
           tables) -- a recurring price table is not an event.
  "topic"  energy, rates, inflation, trade, labour, growth, risk, equities,
           or other
  "dir"    likely SAME-DAY effect on a LONG equity position:
             -1 risk-off   1 risk-on   0 neutral or unclear
           Judge the ECONOMIC implication, not the tone of the words. Higher
           oil, higher yields, higher inflation, rate hikes, tariffs and supply
           disruptions are -1. Their opposites are 1. An attack on energy
           infrastructure is -1 even with no price mentioned; a de-escalation
           is 1.

Return ONLY a JSON array. No prose, no markdown fence."""

# gemini-2.5-flash IS LISTED BY THE MODELS ENDPOINT AND STILL 404s: "no longer
# available to new users". The listing is not an availability check -- the only
# reliable test is a generateContent call with the real key. Probed 2026-09-13:
# 3.6-flash, flash-latest and 3.5-flash also failed; 3.1-flash-lite answered.
#
# REST, not the google-genai SDK: ~900MB of torch and spaCy came off this box
# to keep the models hosted, and adding an SDK to call a hosted model would
# walk that back for nothing.
GEMINI_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("TRADING_GEMINI_MODEL", "gemini-3.1-flash-lite")
GEMINI_URL = ("https://generativelanguage.googleapis.com/v1beta/models/"
              "{model}:generateContent")

# 46 headlines in one request read-timed-out at 120s; 12 is comfortable.
GEMINI_CHUNK = int(os.getenv("TRADING_GEMINI_CHUNK", "12"))
_LLM_DEAD = False


def classify(articles: list) -> list:
    """Gemini decides macro, topic and direction for every article at once.

    Chunked and batched: one request per GEMINI_CHUNK headlines, so a sweep is
    three or four calls rather than one per headline.

    A failure LATCHES and returns what has been classified so far. Everything
    unclassified stays macro=False and votes on nothing, so an outage produces
    no verdict rather than a wrong one.
    """
    global _LLM_DEAD
    for a in articles:
        a.setdefault("is_macro", False)
        a.setdefault("topic", None)
        a.setdefault("rule_dir", 0)
        a.setdefault("rule_why", "")
    if not GEMINI_KEY:
        logger.warning("GEMINI_API_KEY is not set -- no macro read this sweep.")
        return articles

    import json
    import urllib.request

    titles = [a["title"] for a in articles]
    url = GEMINI_URL.format(model=GEMINI_MODEL) + "?key=" + GEMINI_KEY
    for k in range(0, len(titles), GEMINI_CHUNK):
        if _LLM_DEAD:
            break
        part = titles[k:k + GEMINI_CHUNK]
        numbered = "\n".join(f"{k + i}. {t}" for i, t in enumerate(part))
        body = json.dumps({
            "systemInstruction": {"parts": [{"text": GEMINI_SYSTEM}]},
            "contents": [{"parts": [{"text": numbered}]}],
            "generationConfig": {"temperature": 0.0, "maxOutputTokens": 2048,
                                 "responseMimeType": "application/json"},
        }).encode()
        try:
            req = urllib.request.Request(
                url, data=body, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=90) as r:
                out = json.loads(r.read())
            rows = json.loads(
                out["candidates"][0]["content"]["parts"][0]["text"])
        except Exception as exc:
            _LLM_DEAD = True
            logger.warning("Gemini unavailable (%s) -- %d of %d headlines "
                           "classified, the rest vote on nothing.",
                           getattr(exc, "code", None) or type(exc).__name__,
                           k, len(titles), exc_info=True)
            break
        for row in rows:
            try:
                a = articles[int(row["i"])]
            except (KeyError, ValueError, IndexError):
                continue
            a["is_macro"] = bool(row.get("macro"))
            a["topic"] = (row.get("topic") or "other").lower()
            a["rule_dir"] = int(row.get("dir") or 0)
            a["rule_why"] = "gemini"
            a["scored_text"] = a["title"]
    return articles


# --------------------------------------------------------------- 1. fetch


def fetch() -> list:
    """RSS -> feedparser. Every feed in its own try; one down never stops the sweep."""
    import feedparser

    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    out = []
    plan = [{"name": n, "url": u, "macro": True} for n, u in FEEDS]
    plan += [dict(f, macro=False) for f in COMPANY_FEEDS
             if not (f.get("agent") == "" and "agent" in f)]   # EDGAR without a UA is a 403: skip, say so once
    if SEC_USER_AGENT == "" and any("agent" in f for f in COMPANY_FEEDS):
        logger.warning("edgar-*                skipped: TRADING_SEC_USER_AGENT is empty and the SEC "
                       "answers 403 without a contact address")
    for feed in plan:
        name, url = feed["name"], feed["url"]
        try:
            kwargs = {"agent": feed["agent"]} if feed.get("agent") else {}
            entries = feedparser.parse(url, **kwargs).entries or []
        except Exception:
            logger.warning("%-22s unreachable", name, exc_info=True)
            continue
        # A filings feed or an index-release feed is QUIET most of the time --
        # a company files an 8-K every few weeks and S&P announces changes a
        # few times a quarter. Empty or old is the normal state for those, not
        # a broken URL, so the dead/stale warnings apply to the wires only.
        quiet_ok = name.startswith("edgar-") or name == "gnews-spdji" or name == "gnews-index"
        if not entries:
            if not quiet_ok:
                logger.warning("%-22s 0 entries -- feed may be dead", name)
            continue
        prefix = feed.get("prefix") or ""
        newest, kept = None, 0
        for e in entries:
            title = (e.get("title") or "").strip()
            if not title:
                continue
            if prefix:
                title = prefix + title
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
                # Company wires and filings are stored for the ticker grader
                # and kept out of the macro classifier. See COMPANY_FEEDS.
                "macro_eligible": feed["macro"],
            })
            kept += 1
        # A DEAD FEED READS AS QUIET, NOT BROKEN -- the failure mw_marketpulse
        # produced on 2026-09-12. Say it out loud.
        if (newest and not quiet_ok
                and (datetime.now(timezone.utc) - newest) > timedelta(days=3)):
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
                (VERDICT_WINDOW_H,))
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

    company = [a for a in arts if not a.get("macro_eligible", True)]
    arts = [a for a in arts if a.get("macro_eligible", True)]
    if company:
        print(f"{len(company)} company-wire/filing headline(s) stored for the ticker "
              f"grader, not sent to the macro model")
    arts = classify(arts) if arts else []
    macro = [a for a in arts if a["is_macro"]]
    print(f"{len(macro)} macro after the NER filter "
          f"({len(arts) - len(macro)} dropped as single-name or off-topic)\n")
    for a in sorted(macro, key=lambda x: (x["rule_dir"], x["title"]))[:24]:
        d = {1: "RISK_ON ", -1: "RISK_OFF", 0: "  --    "}[a["rule_dir"]]
        print(f"  {d}  {(a.get('topic') or '-')[:9]:<9}  "
              f"[{a['rule_why'][:22]:22s}] {a['title'][:52]}")

    saved = save_scores(arts)
    # THE WINDOW, NOT THIS SWEEP. window_scores() returns every article scored
    # in the last LOOKBACK_HOURS, so a quiet hour still votes on the day's
    # accumulated macro picture. Falls back to this sweep's articles if the
    # store is unreachable, which is strictly worse but never nothing.
    window = window_scores() or macro
    print(f"\nscored {saved} new; verdict over {len(window)} article(s) "
          f"in the last {VERDICT_WINDOW_H}h (fetched {LOOKBACK_HOURS}h)")
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
        # "other" is what Gemini returns when a headline is macro but fits no
        # named topic. Unrelated by construction, so it is shown and not
        # counted -- a large "other" means the topic list needs a name, not
        # that the tape has a new theme.
        note = "   (not counted)" if t == "other" else ""
        print(f"  {t:<10} {arrow}  from {cnt} headline(s){note}")
    print(f"\nMACRO VERDICT  {label.upper()}  net {net:+.3f}  over {n} topic(s)")
    if args.dry_run:
        print("DRY RUN — nothing written.")
        return
    print(f"wrote {store(v, now)} row(s); "
          f"{write_jsonl(arts)} record(s) -> {OUT_PATH}")


if __name__ == "__main__":
    main()
