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
from datetime import date, datetime
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
    "QQQ": ["nasdaq 100", "nasdaq-100", "qqq"],
}

LOOKBACK_DAYS = int(os.getenv("TRADING_NEWS_LOOKBACK_DAYS", "3"))

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
    return ALIASES.get(symbol.upper(), [symbol.lower()])


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
            "ORDER BY publication_date DESC LIMIT 25"
        )
        args = [f"%{p}%" for p in pats] + [f"{int(days)} days"]
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


def same_day_headlines(symbol: str, day: Optional[date] = None) -> List[str]:
    """Headlines for this symbol published on `day` (New York), newest first."""
    day = day or datetime.now(NY).date()
    pats = patterns_for(symbol)
    if not pats:
        return []
    try:
        import psycopg2

        clause = " OR ".join(["headline_text ILIKE %s"] * len(pats))
        sql = (
            "SELECT headline_text FROM market_news_vectors "
            f"WHERE ({clause}) "
            "AND (publication_date AT TIME ZONE 'America/New_York')::date = %s "
            "ORDER BY publication_date DESC LIMIT 25"
        )
        with psycopg2.connect(_dsn()) as conn, conn.cursor() as cur:
            cur.execute(sql, [f"%{p}%" for p in pats] + [day])
            return [r[0] for r in cur.fetchall()]
    except Exception:
        logger.warning("Same-day news lookup failed for %s.", symbol, exc_info=True)
        return []


def classify_day(symbol: str, day: Optional[date] = None) -> dict:
    """{verdict, confidence, rationale, headline_count} for today's news.

    Returns NEUTRAL/0.0 with a zero count when there is no news, and on ANY
    failure -- no headlines is a real answer and an outage must not read as a
    signal. Never raises: section 55 records a headline lookup taking down
    three cycles at the open with seven positions live.
    """
    empty = {"verdict": "NEUTRAL", "confidence": 0.0, "rationale": None, "headline_count": 0}
    heads = same_day_headlines(symbol, day)
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
            "WEIGH THE HEADLINES, DO NOT COUNT THEM. Ten repetitive 'Is X a Buy?' "
            "pieces are not a bullish signal; one credible report of a cancelled "
            "order, a guidance change or an SEC filing outranks all of them. "
            "Syndicated near-duplicates of the same story are ONE event, not many.\n\n"
            f"Headlines published today about {symbol}:\n{listed}"
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
