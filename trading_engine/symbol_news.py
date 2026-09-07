"""Which stored headlines are about which symbol.

THE GAP THIS CLOSES. market_news_vectors has been ingesting and embedding
headlines since the engine was built -- 2,321 rows by 2026-09-06 -- and not
one of them was ever attached to a position. The reason is that the engine
holds TICKERS and the wires write COMPANY NAMES:

    "SNDK"  0 headlines        "SanDisk"          14
    "STX"   0                  "Seagate"           1
    "WDC"   0                  "Western Digital"   2
    "NVDA"  8                  "Nvidia"          165

On 2026-09-04 SanDisk rose 11.9% and two headlines about it were captured,
embedded and stored. Both were invisible to a book holding SNDK, and a short
call was nearly written into that catalyst. The feed was never the problem
and no amount of extra feeds would have helped: the missing piece is this
sixteen-line mapping.

DELIBERATELY OBSERVATIONAL. Nothing here gates an entry. It returns counts
and the latest headline so they can be written beside a weekly_shadow row,
exactly as sig_rv20 and the rest are -- notes, never a gate, until there is
enough tagged history to measure whether a catalyst predicts anything. That
is the same discipline section 22 applied to crude and section 14 to the
macro LLM verdict, and this file's history is unambiguous about what happens
when an unmeasured input starts deciding trades.
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, time as dtime, timedelta
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

NY = ZoneInfo("America/New_York")

# Ticker -> the strings a newswire actually prints. Lowercase; matched
# case-insensitively as a substring, so "sandisk" catches "SanDisk Corp",
# "Sandisk" and "SANDISK".
#
# Keep these SPECIFIC. "dell" is safe; a bare "mu" or "stx" would match
# inside ordinary words and flood the tag with noise, which is why the
# tickers themselves are only used where they are long enough to be
# unambiguous.
ALIASES: Dict[str, List[str]] = {
    "SNDK": ["sandisk", "sndk"],
    "STX": ["seagate", "stx technology"],
    "WDC": ["western digital", "wdc"],
    "MU": ["micron"],
    "NVDA": ["nvidia", "nvda"],
    "DELL": ["dell technologies", "dell"],
    "AVGO": ["broadcom", "avgo"],
    "PANW": ["palo alto networks", "panw"],
    "MRVL": ["marvell", "mrvl"],
    "CRWV": ["coreweave", "crwv"],
    "AMZN": ["amazon", "amzn"],
    "GOOGL": ["alphabet", "googl", "google"],
    "META": ["meta platforms", "facebook"],
    "MSFT": ["microsoft", "msft"],
    # QQQ is deliberately absent -- see MACRO_TERMS.
}

# QQQ IS NOT A COMPANY AND MUST NOT BE READ LIKE ONE.
#
# Matching "qqq" and "nasdaq 100" returns what a ticker feed carries for an
# ETF: fund-comparison articles. On 2026-09-07 QQQ's entire same-day set was
# two pieces about JEPQ versus Invesco's own income fund, graded NEUTRAL 0.85
# -- correctly, because neither is a market event.
#
# What actually moves QQQ is the macro tape: rates, yields, oil, jobs,
# inflation, geopolitics -- IN BOTH DIRECTIONS. See vector 5. The general feeds carry that well and always did.
# Today's set includes "Oil prices rise to 6-week high after Iran and U.S.
# trade blows", "A Fed rate hike is coming into view", "Treasury yields face
# 4.8% test" and "Dow Jones Futures Fall With Iran, Apple, Inflation In Focus"
# -- the whole picture, none of it reachable through a QQQ ticker match.
#
# Kept SPECIFIC rather than broad. "rate" alone matches every mortgage and CD
# story; "rate hike" and "interest rate" do not. A macro read assembled from
# retail-finance noise is worse than none, because it arrives graded.
MACRO_TERMS: List[str] = [
    # 1. CENTRAL BANK & LIQUIDITY
    "federal reserve", "the fed", "fed rate", "fed chair", "fomc", "powell",
    "rate hike", "rate cut", "interest rate", "quantitative easing",
    "quantitative tightening", "balance sheet runoff", "dot plot",

    # 2. GEOPOLITICS & COMMODITY SHOCKS
    # For QQQ the transmission is inflation, not the commodity itself: an oil
    # shock matters because it moves rate expectations, which move the
    # multiple on long-duration tech.
    "iran", "strait of hormuz", "sanctions", "escalation", "crude oil",
    "oil price", "opec", "brent", "supply disruption", "export ban",
    "taiwan strait", "chip export",
    # ...AND THE RESOLUTION OF ONE. Every term above fires on trouble only, so
    # the feed could report a conflict starting and never report it ending.
    "ceasefire", "de-escalation", "peace talks", "sanctions relief",
    "sanctions lifted", "truce", "supply glut", "output increase",

    # 3. SOVEREIGN DEBT & FIXED INCOME
    # Global yields, not just the US 10-year: a JGB or Bund repricing pulls
    # capital out of duration everywhere, and QQQ is a duration trade.
    "treasury yield", "10-year", "bond yield", "yield curve", "inversion",
    "global bond", "bunds", "jgb", "gilt", "term premium", "auction tailed",
    # "inversion" and "auction tailed" name a stress with no opposite in the
    # list. These are the opposites.
    "curve steepen", "auction stopped through", "bond rally",
    "credit spreads tighten", "yields fall", "yields ease",

    # 4. SYSTEMIC ECONOMIC DATA
    "cpi", "core inflation", "pce", "non-farm payroll", "nonfarm payroll",
    "nfp", "jobs report", "unemployment rate", "jobless claims",
    "retail sales", "ism ", "gdp",

    # 5. EASING, DISINFLATION AND GROWTH
    #
    # THE SIDE THE FIRST FOUR VECTORS CANNOT PRODUCE. Measured 2026-09-07: the
    # QQQ read came back BEARISH on 10 of 14 sessions, 5/10 on next-session
    # direction, and the tilt survived the tape reversing -- five straight
    # bearish reads while QQQ printed +0.22, +0.04, +0.30, -0.05. Dropping
    # re-reports changed five verdicts in BOTH directions and left the
    # distribution at 10/6/1, which ruled repetition out (section 126).
    #
    # What was left is retrieval. A term set assembled from tightening,
    # conflict, debt stress and hard data can only ever hand the classifier
    # trouble, and a classifier given only trouble reports trouble daily. The
    # bias was never in the model; it was in what the model was allowed to see.
    #
    # Kept as specific as the rest. "rally" and "record high" would drag in
    # every retail-finance piece on the wire, which is the failure this file
    # already warns about two comments above.
    "soft landing", "disinflation", "inflation cools", "cooling inflation",
    "rate cuts", "dovish", "hawkish", "easing cycle", "fed pivot",
    "liquidity injection", "goldilocks", "risk-on", "risk appetite",
    "earnings upgrade", "guidance raised", "capex cycle", "productivity boom",

    # Index-level tape, which is the outcome these five vectors produce
    "nasdaq futures", "dow jones futures", "s&p 500 futures", "stock futures",
]

LOOKBACK_DAYS = int(os.getenv("TRADING_NEWS_LOOKBACK_DAYS", "3"))

# Headlines handed to the classifier for one symbol-day. 25 was fine for a
# single ticker and truncates the macro read: 2026-09-04 matched exactly 25
# macro terms, which is the cap, not the count. A truncated macro day drops
# whichever vector sorts last and can flip a verdict for no reason.
MAX_HEADLINES = int(os.getenv("TRADING_NEWS_MAX_HEADLINES", "45"))

# Haiku, not Opus, and the switch is an ENV VAR so reverting costs no deploy.
#
# Headline sentiment is a classification, which is the task Haiku is built for,
# and it was checked rather than assumed. On the 2026-09-04 SanDisk headlines
# -- the exact case this whole feature exists for -- all three models returned
# the same answer with the same reasoning:
#
#     claude-opus-5     NEUTRAL  conf 0.70  2.7s  "both items are backward-looking"
#     claude-haiku-4-5  NEUTRAL  conf 0.95  1.5s  "merely lists SNDK in a round-up"
#     claude-sonnet-5   NEUTRAL  conf 0.85  2.8s  "generic round-up ... already-occurred"
#
# Haiku is $1/$5 per MTok against Opus at $5/$25, so this is 5x cheaper on the
# one call that scales with news volume. ONE TEST CASE IS NOT VALIDATION: if a
# verdict ever looks wrong, set TRADING_NEWS_MODEL=claude-opus-5 and compare
# before concluding the prompt is at fault.
NEWS_MODEL = os.getenv("TRADING_NEWS_MODEL", "claude-haiku-4-5")


def patterns_for(symbol: str) -> List[str]:
    """Match strings for a symbol. QQQ resolves to the macro tape, not to
    articles that happen to name the ETF."""
    sym = symbol.upper()
    if sym == "QQQ":
        return MACRO_TERMS
    return ALIASES.get(sym, [sym.lower()])


def _dsn() -> str:
    url = os.getenv("DATABASE_URL", "")
    return url.replace("postgresql+psycopg2://", "postgresql://").replace(
        "postgresql+asyncpg://", "postgresql://")


def recent_headlines(symbol: str, days: int = None) -> List[Tuple[str, object]]:
    """(headline, published) for this symbol within `days`, newest first.

    Returns [] on any failure. This is a note-taking path that runs beside a
    live entry, and section 55 records what a headline lookup raising inside
    the cycle cost: three whole cycles at the open with seven positions live.
    """
    days = LOOKBACK_DAYS if days is None else days
    pats = patterns_for(symbol)
    if not pats:
        return []
    try:
        import psycopg2

        clause = " OR ".join(["headline_text ILIKE %s"] * len(pats))
        sql = (
            "SELECT headline_text, publication_date FROM market_news_vectors "
            f"WHERE ({clause}) AND publication_date > now() - interval %s "
            "ORDER BY publication_date DESC LIMIT %s"
        )
        args = [f"%{p}%" for p in pats] + [f"{int(days)} days", MAX_HEADLINES]
        with psycopg2.connect(_dsn()) as conn, conn.cursor() as cur:
            cur.execute(sql, args)
            return [(r[0], r[1]) for r in cur.fetchall()]
    except Exception:
        logger.warning("News lookup failed for %s — continuing without it.", symbol,
                       exc_info=True)
        return []


def news_signals(symbol: str, days: int = None) -> dict:
    """Columns for one weekly_shadow row. Empty dict when nothing is known.

    sig_news_count_3d is the headline COUNT, not a sentiment read. A count is
    a fact about attention; sentiment would be a model, and an unmeasured
    model has no business next to measured columns. A spike in count is the
    thing that would have flagged SanDisk on 2026-09-04 without anyone having
    to be right about whether the news was good.
    """
    rows = recent_headlines(symbol, days)
    if not rows:
        return {"sig_news_count_3d": 0, "sig_news_latest": None}
    return {
        "sig_news_count_3d": len(rows),
        "sig_news_latest": rows[0][0][:500],
    }


# ---------------------------------------------------------------------------
# Same-day sentiment
# ---------------------------------------------------------------------------
#
# WHY SAME-DAY ONLY. A catalyst is priced within the session it breaks. The
# 2026-09-04 SanDisk headlines moved the stock 11.9% THAT DAY; by Monday they
# are history the chart already contains, and counting them again would double
# count a move the technicals are describing. So the window is the trading day,
# not a rolling lookback -- an empty verdict on a quiet morning is the correct
# answer, not a missing one.
#
# WHY GRADED, NOT BINARY. The instruction that produced this was "very bullish
# very bearish", and that is the useful distinction: an ordinary broker note is
# not the same event as an 11.9% gap, and a structure decision that treats them
# alike will write short calls into catalysts. VERY_* is reserved for news that
# re-rates the name, not news that merely reads positive.


class NewsSentiment(BaseModel):
    """One trading day's news read for one symbol."""

    verdict: str = Field(
        description=(
            "One of VERY_BULLISH, BULLISH, NEUTRAL, BEARISH, VERY_BEARISH. "
            "Reserve VERY_* for news that re-rates the company -- M&A, guidance "
            "changes, supply shocks, a major customer win or loss. Ordinary "
            "coverage, analyst chatter and 'stocks on the move' round-ups are "
            "NEUTRAL unless they carry such news."
        )
    )
    confidence: float = Field(description="0.0 to 1.0.")
    rationale: str = Field(description="One sentence, citing the headline that decided it.")



# A RECURRING THEME IS NOT A NEW EVENT. The wires re-report the same standing
# macro story every morning -- Iran, the Fed's next move, yields testing a
# level -- and those headlines land inside the 09:30 window looking fresh. The
# market priced them the day they broke. Grading them again every session is
# what gave the QQQ read a standing bearish tilt: BEARISH on 10 of 14 sessions,
# 5/10 on direction, and unmoved while the tape reversed (section 125).
#
# So a headline counts only if it is not a near-duplicate of something this
# symbol already carried in the trailing window. The embeddings needed for that
# are already stored, so this costs one query and no model calls.
NOVELTY_LOOKBACK_DAYS = int(os.getenv("TRADING_NEWS_NOVELTY_DAYS", "10"))
# Cosine similarity above which a headline is treated as a re-report.
#
# CHOSEN AT THE 75th PERCENTILE of the observed similarity-to-prior-coverage
# distribution (median 0.767, p75 0.826, p90 0.868), BEFORE the effect on
# forward returns was measured -- so the AUC below is a check on the choice,
# not the thing that made it. 0.88 was the first guess and dropped only 7%:
# these headlines are not duplicates, they are the same standing themes in
# different words, which is exactly what a market has already priced.
#
# THE COST OF THIS FILTER IS A FOLLOW-UP. A genuine development on a story
# already covered -- "index funds must now buy $X billion of SNDK" the day
# after the inclusion itself -- scores similar to its predecessor and can be
# dropped with it. That is the trade being made, and it is the reason this is
# a threshold rather than an exact-match rule.
NOVELTY_THRESHOLD = float(os.getenv("TRADING_NEWS_NOVELTY", "0.83"))


def previous_session_close(day: date) -> datetime:
    """16:00 ET on the trading day before `day`, holidays included."""
    try:
        from .market_calendar import close_time_for, is_trading_day
    except Exception:
        def is_trading_day(d):
            return d.weekday() < 5

        def close_time_for(d):
            return dtime(16, 0)
    d = day - timedelta(days=1)
    for _ in range(10):
        if is_trading_day(d):
            break
        d -= timedelta(days=1)
    # close_time_for knows the half days: the Friday after Thanksgiving ends at
    # 13:00, and three extra hours of wire copy belong to the NEXT session.
    return datetime.combine(d, close_time_for(d), tzinfo=NY)


def session_headlines(symbol: str, day: Optional[date] = None,
                      cutoff: Optional[dtime] = None,
                      novel_only: bool = True,
                      with_scores: bool = False):
    """Headlines for this symbol SINCE THE PREVIOUS SESSION'S CLOSE.

    NOT the calendar day, which was the original filter and was wrong. A
    catalyst that breaks after the bell or over a weekend is unpriced when the
    next session opens, and a same-calendar-day filter skips it: SanDisk's S&P
    100 inclusion was published 2026-09-04 at 22:11 ET, so a Monday morning
    read asking for "today's headlines" would have missed the single largest
    catalyst on the book.

    The window is [previous session close, end of `day`], or up to `cutoff` on
    `day` when one is given. Backtests MUST pass a cutoff -- reading a whole
    session's headlines to grade its open is lookahead, and it is the reason
    same-day agreement looked so good: the headlines were reporting the move.

    AND ONLY WHAT IS NEW IN IT. With novel_only, a headline is dropped when it
    is within NOVELTY_THRESHOLD cosine of something this symbol already carried
    in the previous NOVELTY_LOOKBACK_DAYS. A market prices a story when it
    breaks; a wire re-reporting it the next morning is not a second event, and
    counting it as one is how a standing narrative turns into a daily verdict.

    with_scores returns (headline, similarity_to_prior) so the threshold can be
    inspected rather than trusted.
    """
    day = day or datetime.now(NY).date()
    start = previous_session_close(day)
    end = (datetime.combine(day, cutoff, tzinfo=NY) if cutoff
           else datetime.combine(day, dtime(23, 59, 59), tzinfo=NY))
    pats = patterns_for(symbol)
    if not pats:
        return []
    try:
        import psycopg2

        clause = " OR ".join(["headline_text ILIKE %s"] * len(pats))
        likes = [f"%{p}%" for p in pats]
        if not novel_only:
            sql = (
                "SELECT headline_text FROM market_news_vectors "
                f"WHERE ({clause}) "
                "AND publication_date > %s AND publication_date <= %s "
                "ORDER BY publication_date DESC LIMIT %s"
            )
            with psycopg2.connect(_dsn()) as conn, conn.cursor() as cur:
                cur.execute(sql, likes + [start, end, MAX_HEADLINES])
                rows = [(r[0], 0.0) for r in cur.fetchall()]
            return rows if with_scores else [h for h, _ in rows]

        # Each headline in the window scored against everything this symbol
        # carried in the lookback. max() over an empty prior set is NULL, which
        # COALESCE turns into 0.0 -- no prior coverage means everything is new,
        # which is the correct reading and not an error.
        sql = (
            "WITH win AS ("
            "  SELECT headline_text, text_embedding FROM market_news_vectors"
            f"  WHERE ({clause}) AND publication_date > %s AND publication_date <= %s"
            "  ORDER BY publication_date DESC LIMIT %s"
            "), prior AS ("
            "  SELECT text_embedding FROM market_news_vectors"
            f"  WHERE ({clause}) AND publication_date <= %s"
            "    AND publication_date > %s - make_interval(days => %s)"
            ") "
            "SELECT w.headline_text, COALESCE(("
            "  SELECT max(1 - (w.text_embedding <=> p.text_embedding)) FROM prior p"
            "), 0.0) AS prior_sim "
            "FROM win w ORDER BY prior_sim ASC"
        )
        params = (likes + [start, end, MAX_HEADLINES]
                  + likes + [start, start, NOVELTY_LOOKBACK_DAYS])
        with psycopg2.connect(_dsn()) as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            scored = [(r[0], float(r[1] or 0.0)) for r in cur.fetchall()]
        fresh = [(h, sim) for h, sim in scored if sim < NOVELTY_THRESHOLD]
        if scored and not fresh:
            logger.info("%s: all %d headlines in the window are re-reports of "
                        "stories already carried; nothing new to grade.",
                        symbol, len(scored))
        return fresh if with_scores else [h for h, _ in fresh]
    except Exception:
        logger.warning("Session news lookup failed for %s.", symbol, exc_info=True)
        return []


def classify_day(symbol: str, day: Optional[date] = None,
                 cutoff: Optional[dtime] = None) -> dict:
    """{verdict, confidence, rationale, headline_count} for the news a trader
    could have read at this session's open -- everything published since the
    previous close, which is the set that is NOT yet in the price.

    Returns NEUTRAL/0.0 with a zero count when there is no news, and on ANY
    failure -- no headlines is a real answer and an outage must not read as a
    signal. Never raises: section 55 records a headline lookup taking down
    three cycles at the open with seven positions live.
    """
    empty = {"verdict": "NEUTRAL", "confidence": 0.0, "rationale": None, "headline_count": 0}
    heads = session_headlines(symbol, day, cutoff)
    if not heads:
        return empty
    try:
        from langchain_anthropic import ChatAnthropic

        llm = ChatAnthropic(
            model=NEWS_MODEL, max_tokens=1024,
        ).with_structured_output(NewsSentiment)
        listed = "\n".join(f"- {h}" for h in heads[:25])
        out = llm.invoke(
            f"You are classifying one trading day's news for {symbol} for an options "
            "trading system that will size a weekly vertical spread on the answer.\n\n"
            "Judge ONLY the direct implication for this company's share price over the "
            "next five trading days. A headline that merely mentions the ticker in a "
            "round-up of movers is NEUTRAL. A headline describing an already-completed "
            "move is NEUTRAL -- the move is in the price. Reserve VERY_BULLISH and "
            "VERY_BEARISH for news that re-rates the business.\n\n"
            "THESE HEADLINES WERE PUBLISHED SINCE THE PREVIOUS SESSION'S CLOSE AND "
            "ARE NOT RE-REPORTS. Near-duplicates of stories this name already "
            "carried in the last two weeks have been removed before you see them, "
            "so what remains is what is NEW this morning and not yet in the price. "
            "Judge it as new information. A story that merely recaps what the LAST "
            "session already did is still NEUTRAL: that move is priced.\n\n"
            "WEIGH THE HEADLINES, DO NOT COUNT THEM. Ten repetitive 'Is X a Buy?' "
            "pieces are not a bullish signal; one credible report of a cancelled "
            "order, a guidance change or an SEC filing outranks all of them. "
            "Syndicated near-duplicates of the same story are ONE event, not many.\n\n"
            f"New headlines about {symbol} since the previous close:\n{listed}"
        )
        return {
            "verdict": out.verdict,
            "confidence": float(out.confidence),
            "rationale": out.rationale,
            "headline_count": len(heads),
        }
    except Exception:
        logger.warning("News classification failed for %s.", symbol, exc_info=True)
        return {**empty, "headline_count": len(heads)}


# Retained so older callers keep working. The session window is the correct
# one; this alias exists only so a stale import does not fail silently.
same_day_headlines = session_headlines
