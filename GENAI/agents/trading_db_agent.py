"""The trading-database agent: a question in English, one read-only query out.

Section 200. The GENAI supervisor had two specialists, CSV and PDF, and no
way to ask about the trading book. This node answers questions against the
trading tables in Postgres and the news vectors beside them, in three steps:

    1. Gemini writes ONE SELECT against a schema it is shown, from a
       whitelist of trading and news tables (never users, customers, tokens).
    2. The query is guarded -- a single SELECT or WITH, no write verbs, only
       whitelisted tables, a LIMIT -- and run inside a READ ONLY transaction
       with a 10-second statement timeout.
    3. If the question is about news, the same question is embedded with
       voyage-4 and cosine-searched against market_news_vectors (1024 dims,
       the same model and width GENAI's document store uses), and Gemini
       answers from the rows and the headlines, quoting the SQL it ran.

The database never sees anything but a guarded SELECT. The model never sees
credentials. A failed guard is an answer ("refused: ..."), not an exception.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, List, Optional

from GENAI.gemini_llm import agenerate

from .state import SupervisorState

logger = logging.getLogger(__name__)

TABLES = [
    "trading_history", "trading_history_archive", "trading_open_positions", "trading_logs",
    "news_verdicts", "news_verdict_history", "news_verdict_outcomes", "news_symbol_impact",
    "news_seen", "market_news_vectors", "weekly_shadow", "dte0_shadow", "index_events",
    "macro_session_outcomes", "trading_macro_readings", "trading_macro_verdicts",
    "trading_breadth_readings", "symbol_sentiment_hourly", "trade_setup_vectors",
]
HIDDEN_COLUMNS = {"text_embedding", "embedding"}      # 1024 floats help nobody read
ROW_LIMIT = int(os.getenv("GENAI_DB_ROW_LIMIT", "200"))
TIMEOUT_MS = int(os.getenv("GENAI_DB_TIMEOUT_MS", "10000"))
NEWS_WORDS = re.compile(r"\b(news|headline|headlines|article|articles|story|stories|said|report|"
                        r"announce|announced|rumou?r|catalyst|sentiment|verdict)\b", re.I)
_WRITE = re.compile(r"\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|copy|"
                    r"vacuum|analyze|call|do|execute|set|reset|listen|notify|lock|comment|"
                    r"refresh|reindex|cluster|security|into)\b", re.I)
_TABLE_REF = re.compile(r"\b(?:from|join)\s+([a-zA-Z_][\w.]*)", re.I)

_SCHEMA_CACHE: Dict[str, str] = {}

# What each table IS, because a column list does not say. The first live run
# answered "what news did the pipeline record about Sandisk" from
# news_symbol_impact, whose sentiment columns are mostly empty, when the graded
# read lives in news_verdicts (section 200).
TABLE_NOTES = {
    "trading_history": "every closed spread: entry/exit, realized P&L, close_reason (the exit rule), opened_at/closed_at",
    "trading_history_archive": "older closed spreads, same shape",
    "trading_open_positions": "the engine's own open row (QQQ book)",
    "trading_logs": "per-cycle engine log rows with indicators",
    "news_verdicts": "THE graded per-symbol news read, one row per symbol per trading_day: verdict (VERY_BULLISH..VERY_BEARISH), confidence, rationale, headline_count",
    "news_verdict_history": "every hourly re-grade behind news_verdicts (asof)",
    "news_verdict_outcomes": "what the underlying did after each verdict (ret_pct, move_atr)",
    "news_symbol_impact": "individual Polygon articles per symbol with the move after them; sentiment columns are often empty",
    "news_seen": "raw RSS headlines by source (benzinga, gnews-*, edgar-*, cnbc...), title, published",
    "market_news_vectors": "embedded headlines (headline_text, publication_date, source); searched by similarity, not by SQL text match",
    "weekly_shadow": "weekly credit-spread shadow book rows with entry signals (sig_*) and expiry_return_pct",
    "dte0_shadow": "same-day shadow rows with peak/worst/expiry returns",
    "index_events": "index inclusion/removal events: symbol, index_name, action, announced_at, effective_date",
    "macro_session_outcomes": "the macro verdict per session and what QQQ did",
    "trading_macro_readings": "crude/10Y/VIX objective macro reads",
    "trading_macro_verdicts": "the text macro verdict per day",
    "trading_breadth_readings": "market breadth reads",
    "symbol_sentiment_hourly": "hourly per-symbol sentiment scores by source",
    "trade_setup_vectors": "embedded trade setups",
}


def _dsn() -> str:
    url = os.getenv("DATABASE_URL", "")
    return url.replace("postgresql+psycopg2://", "postgresql://").replace(
        "postgresql+asyncpg://", "postgresql://")


def schema_text(conn) -> str:
    """One line per whitelisted table with its columns, from information_schema."""
    if "schema" in _SCHEMA_CACHE:
        return _SCHEMA_CACHE["schema"]
    with conn.cursor() as cur:
        cur.execute("""SELECT table_name, column_name, data_type FROM information_schema.columns
                       WHERE table_schema='public' AND table_name = ANY(%s)
                       ORDER BY table_name, ordinal_position""", (TABLES,))
        cols: Dict[str, List[str]] = {}
        for t, c, d in cur.fetchall():
            if c in HIDDEN_COLUMNS:
                continue
            cols.setdefault(t, []).append(f"{c} {d}")
    lines = [f"{t}({', '.join(cs)})" + (f"  -- {TABLE_NOTES[t]}" if t in TABLE_NOTES else "")
             for t, cs in cols.items()]
    _SCHEMA_CACHE["schema"] = "\n".join(lines)
    return _SCHEMA_CACHE["schema"]


def guard(sql: str) -> "tuple[Optional[str], Optional[str]]":
    """(safe_sql, None) or (None, why_refused). Pure; tested."""
    s = (sql or "").strip().rstrip(";").strip()
    if not s:
        return None, "empty query"
    if ";" in s:
        return None, "one statement only"
    head = s.split(None, 1)[0].lower()
    if head not in ("select", "with"):
        return None, f"only SELECT is allowed, got {head.upper()}"
    if _WRITE.search(re.sub(r"'[^']*'", "''", s)):
        return None, "write or admin verb present"
    refs = {r.split(".")[-1].lower() for r in _TABLE_REF.findall(s)}
    ctes = {m.lower() for m in re.findall(r"\b([a-zA-Z_]\w*)\s+as\s*\(", s, re.I)}
    bad = [r for r in refs if r not in TABLES and r not in ctes]
    if bad:
        return None, f"table(s) not allowed: {', '.join(sorted(bad))}"
    if not re.search(r"\blimit\s+\d+", s, re.I):
        s += f" LIMIT {ROW_LIMIT}"
    return s, None


def run_readonly(conn, sql: str) -> "tuple[List[str], List[tuple]]":
    with conn.cursor() as cur:
        cur.execute("BEGIN READ ONLY")
        try:
            cur.execute(f"SET LOCAL statement_timeout = {TIMEOUT_MS}")
            cur.execute(sql)
            cols = [d[0] for d in cur.description] if cur.description else []
            rows = cur.fetchmany(ROW_LIMIT)
        finally:
            cur.execute("ROLLBACK")
    return cols, rows


async def _news_context(conn, query: str, top_k: int = 8) -> str:
    """Nearest headlines in market_news_vectors by voyage-4 cosine similarity."""
    try:
        from GENAI.vector_stores import embedding_factory
        vec = embedding_factory("voyage").embed_query(query)
    except Exception as exc:
        return f"(news vector search unavailable: {type(exc).__name__})"
    with conn.cursor() as cur:
        cur.execute("BEGIN READ ONLY")
        try:
            cur.execute(f"SET LOCAL statement_timeout = {TIMEOUT_MS}")
            cur.execute("""SELECT publication_date::timestamp(0), source, headline_text,
                                  round((1 - (text_embedding <=> %s::vector))::numeric, 3)
                           FROM market_news_vectors
                           ORDER BY text_embedding <=> %s::vector LIMIT %s""",
                        (str(vec), str(vec), top_k))
            rows = cur.fetchall()
        finally:
            cur.execute("ROLLBACK")
    return "\n".join(f"{r[0]} [{r[1]}] ({r[3]}) {r[2]}" for r in rows) or "(no headlines)"


def _fmt(cols: List[str], rows: List[tuple]) -> str:
    if not cols:
        return "(no columns)"
    out = [" | ".join(cols)]
    for r in rows[:ROW_LIMIT]:
        out.append(" | ".join("" if v is None else str(v)[:60] for v in r))
    return "\n".join(out)


SQL_SYSTEM = (
    "You write ONE PostgreSQL SELECT statement that answers a question about an options-trading "
    "book. Use only the tables and columns listed. Prefer aggregates and ORDER BY for 'best/worst' "
    "questions. Dates are timestamptz; 'today' means CURRENT_DATE in America/New_York. Never modify "
    "data. Return JSON: {\"sql\": \"...\", \"explanation\": \"one sentence\"}."
)
ANSWER_SYSTEM = (
    "You answer a trader's question from query results and, when given, related headlines. Be "
    "concrete: numbers, names, dates. If the results do not answer the question, say what they do "
    "show. End with a line 'SQL: <the query>'."
)


async def run_trading_db_agent(state: SupervisorState) -> dict:
    import json
    import psycopg2
    q = state["query"]
    try:
        conn = psycopg2.connect(_dsn())
    except Exception as exc:
        return {"db_answer": f"database unavailable: {type(exc).__name__}", "db_sql": None}
    try:
        schema = schema_text(conn)
        raw = await agenerate(f"Schema:\n{schema}\n\nQuestion: {q}", system=SQL_SYSTEM,
                              max_tokens=600, temperature=0.0, json_mode=True)
        try:
            plan = json.loads(raw)
            sql = plan.get("sql", "") if isinstance(plan, dict) else ""
        except Exception:
            sql = raw
        safe, why = guard(sql)
        if not safe:
            return {"db_answer": f"refused: {why}. The model proposed: {sql[:300]}", "db_sql": sql}
        try:
            cols, rows = run_readonly(conn, safe)
        except Exception as exc:
            return {"db_answer": f"query failed: {str(exc).splitlines()[0][:200]}", "db_sql": safe}
        table = _fmt(cols, rows)
        news = await _news_context(conn, q) if NEWS_WORDS.search(q) else ""
        prompt = (f"Question: {q}\n\nQuery run:\n{safe}\n\nResults ({len(rows)} rows):\n{table}\n"
                  + (f"\nRelated headlines (nearest by embedding):\n{news}\n" if news else ""))
        answer = await agenerate(prompt, system=ANSWER_SYSTEM, max_tokens=900, temperature=0.2)
        return {"db_answer": answer, "db_sql": safe, "db_rows": len(rows)}
    finally:
        conn.close()
