"""Index inclusions as dated events, from the headlines the sweep already stores.

Built 2026-09-19 (sections 195, 197). SanDisk's S&P 100 inclusion was
announced 2026-09-04 at 22:11 ET and graded BULLISH that night and
VERY_BULLISH on 09-07 and 09-08. The grade was used once, to refuse a short
call, and never proposed the long. The stock ran from the low 1500s to 1793
between the announcement and the effective close on 09-18, which is the
documented pattern: the demand is scheduled, the buying is mechanical, and
the effective-day close is the END of it.

THIS MODULE turns such a headline into a row: symbol, index, action,
announcement time, effective date. scripts/index_event_trade.py reads the
rows. Nothing here trades.

DETECTION IS ON THE NAMES BEFORE THE VERB. "Bloom Energy, Illumina, and
Everpure Set to Join S&P 500" names three joiners in the segment before "Set
to Join"; "This Little-Known AI Storage Stock Will Join the S&P 500 (Not
Micron or Sandisk)" names none we trade in that segment, and the two names
in the parenthesis are exactly the ones that must NOT match. So the aliases
are searched only in the text preceding the verb, never in the whole title.

EFFECTIVE DATE is the close on which index funds buy, which is the last
session BEFORE the change takes effect. S&P's quarterly rebalance takes
effect prior to the open on the Monday after the third Friday of March,
June, September and December, so an announcement in one of those months at
least three days before that Friday resolves to the third Friday. Anything
else -- an ad-hoc replacement after an acquisition -- is assumed to take a
week: announcement + 7 days, pulled back to the preceding weekday. Neither
is read from the headline because headlines do not carry the date; both are
stated assumptions and the row records which one was used.
"""

from __future__ import annotations

import calendar
import logging
import re
from datetime import date, datetime, timedelta

from .symbol_news import ALIASES

logger = logging.getLogger(__name__)

INDEX_RX = r"(?P<index>s&p\s?500|s&p\s?100|nasdaq[- ]100|s&p\s?midcap\s?400|s&p\s?smallcap\s?600)"
_VERB_JOIN = (r"(?:is |are )?(?:set to join|to join|will join|joins|joining|added to|"
              r"to be added to|will be added to|to replace|will replace|replaces|replacing|promoted to)")
_VERB_LEAVE = r"(?:to leave|will leave|leaves|leaving|removed from|to be removed from|dropped from|exits?|exit from)"
JOIN_RX = re.compile(r"(?P<names>.+?)\s+" + _VERB_JOIN + r"\s+(?:the\s+)?" + INDEX_RX, re.I)
LEAVE_RX = re.compile(r"(?P<names>.+?)\s+" + _VERB_LEAVE + r"\s+(?:the\s+)?" + INDEX_RX, re.I)
# "SanDisk's inclusion in the S&P 100", "Sandisk inclusion to S&P 100"
INCL_RX = re.compile(r"(?P<names>[A-Za-z][\w.&' -]{1,60}?)(?:'s|s')?\s+(?:inclusion|addition)\s+"
                     r"(?:in|to|into)\s+(?:the\s+)?" + INDEX_RX, re.I)
# "... S&P 100 with SanDisk's inclusion ...": the possessive form, with the
# index named anywhere else in the title.
INCL_POSS_RX = re.compile(r"(?P<names>[A-Za-z][\w.& -]{1,40}?)(?:'s|s')\s+(?:inclusion|addition)", re.I)
INDEX_ANY_RX = re.compile(INDEX_RX, re.I)

QUARTER_MONTHS = {3, 6, 9, 12}
UNIVERSE = tuple(s for s in ALIASES if s.upper() != "QQQ")


def _norm_index(s: str) -> str:
    s = re.sub(r"\s+", " ", s.strip().lower()).replace("nasdaq 100", "nasdaq-100")
    return {"s&p500": "S&P 500", "s&p 500": "S&P 500", "s&p100": "S&P 100", "s&p 100": "S&P 100",
            "nasdaq-100": "Nasdaq-100", "s&p midcap 400": "S&P MidCap 400",
            "s&p midcap400": "S&P MidCap 400", "s&p smallcap 600": "S&P SmallCap 600",
            "s&p smallcap600": "S&P SmallCap 600"}.get(s, s.upper())


def _strip_source(title: str) -> str:
    """Google News titles end ' - Source'; drop the suffix before matching."""
    return re.sub(r"\s+-\s+[A-Za-z][\w.&' ]{1,40}$", "", title or "").strip()


def symbols_in(text: str) -> list:
    """Universe symbols whose alias appears in `text`, on word boundaries."""
    low = (text or "").lower()
    out = []
    for sym in UNIVERSE:
        for alias in ALIASES.get(sym, []):
            if re.search(r"(?<![a-z0-9])" + re.escape(alias.lower()) + r"(?![a-z0-9])", low):
                out.append(sym)
                break
    return out


def detect(title: str) -> list:
    """[(symbol, index, action)] for one headline. Empty for anything else."""
    t = _strip_source(title)
    found = []
    for rx, action in ((JOIN_RX, "join"), (INCL_RX, "join"), (LEAVE_RX, "leave")):
        m = rx.search(t)
        if not m:
            continue
        for sym in symbols_in(m.group("names")):
            found.append((sym, _norm_index(m.group("index")), action))
    if not found:
        m = INCL_POSS_RX.search(t)
        mi = INDEX_ANY_RX.search(t)
        if m and mi:
            for sym in symbols_in(m.group("names")):
                found.append((sym, _norm_index(mi.group("index")), "join"))
    # one row per (symbol, index, action)
    seen, out = set(), []
    for f in found:
        if f not in seen:
            seen.add(f)
            out.append(f)
    return out


def third_friday(year: int, month: int) -> date:
    c = calendar.Calendar()
    fridays = [d for d in c.itermonthdates(year, month)
               if d.month == month and d.weekday() == 4]
    return fridays[2]


def effective_date(announced: date) -> "tuple[date, str]":
    """(the close on which the funds buy, which assumption produced it)."""
    if announced.month in QUARTER_MONTHS:
        tf = third_friday(announced.year, announced.month)
        if timedelta(days=3) <= (tf - announced) <= timedelta(days=21):
            return tf, "quarterly rebalance: third Friday"
    d = announced + timedelta(days=7)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d, "ad hoc: announcement + 7 days, previous weekday"


def scan(rows: list) -> list:
    """rows: (guid, source, title, published_or_None) -> event dicts."""
    out = []
    for guid, source, title, published in rows:
        for sym, idx, action in detect(title):
            ann = published or datetime.utcnow()
            ann_d = ann.date() if hasattr(ann, "date") else ann
            eff, basis = effective_date(ann_d)
            out.append({"symbol": sym, "index_name": idx, "action": action,
                        "announced_at": ann, "effective_date": eff, "basis": basis,
                        "headline": (title or "")[:300], "guid": (guid or "")[:500],
                        "source": source})
    return out


def record(conn, events: list) -> int:
    """Insert new events; (symbol, index, action, effective_date) is unique."""
    if not events:
        return 0
    n = 0
    with conn.cursor() as cur:
        for e in events:
            cur.execute(
                """INSERT INTO index_events (symbol, index_name, action, announced_at,
                                             effective_date, basis, headline, guid, source)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (symbol, index_name, action, effective_date) DO NOTHING""",
                (e["symbol"], e["index_name"], e["action"], e["announced_at"],
                 e["effective_date"], e["basis"], e["headline"], e["guid"], e["source"]))
            n += cur.rowcount
    conn.commit()
    for e in events:
        logger.warning("INDEX EVENT %s %s %s: announced %s, effective close %s (%s) — %s",
                       e["symbol"], e["action"].upper(), e["index_name"],
                       str(e["announced_at"])[:16], e["effective_date"], e["basis"],
                       e["headline"][:90])
    return n


def active(conn, today: "date | None" = None) -> list:
    """Join events whose effective close has not passed."""
    today = today or date.today()
    with conn.cursor() as cur:
        cur.execute("""SELECT id, symbol, index_name, announced_at, effective_date, basis,
                              headline, traded_order_id
                       FROM index_events
                       WHERE action='join' AND effective_date >= %s
                       ORDER BY effective_date, symbol""", (today,))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def scan_and_record(conn, rows: list) -> int:
    try:
        return record(conn, scan(rows))
    except Exception:
        logger.exception("index_events: scan failed — headlines were still stored.")
        return 0
