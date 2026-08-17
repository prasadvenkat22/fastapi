"""pgvector-backed storage/lookup for closed-trade setups (entry signals +
outcome) — same asyncpg + cosine-distance pattern as
trading_engine/vector_store.py's headline store, applied to trade setups.

Pure logging today: store_trade_setup() is called from
trading_engine/service.py whenever a trade closes. query_similar_setups()
exists so a similarity-based veto gate can be wired into
execution_risk_agent as a follow-up, once enough real closed trades exist
for it to mean anything — with a handful of trades or fewer, "3 similar
past setups all lost" is noise, not signal, so this deliberately isn't
called from the live decision path yet."""

import os
import uuid
from typing import List, Optional

import asyncpg
from pgvector.asyncpg import register_vector

from GENAI.vector_stores import VoyageEmbeddings

EMBEDDING_DIM = 1024  # matches voyage-4


def build_setup_text(
    strategy: str,
    macd_signal: Optional[str],
    sma_trend: Optional[str],
    bollinger_zone: Optional[str],
    rsi_zone: Optional[str],
    realized_pnl_pct: float,
    close_reason: str,
) -> str:
    """A short natural-language summary of the entry setup + outcome — this
    is what gets embedded, so future similar setups can be retrieved by
    meaning rather than exact field matches."""
    outcome = "a win" if realized_pnl_pct > 0 else "a loss" if realized_pnl_pct < 0 else "breakeven"
    return (
        f"{strategy} entered on MACD={macd_signal}, EMA trend={sma_trend}, "
        f"Bollinger={bollinger_zone}, RSI={rsi_zone}. Closed via {close_reason} "
        f"for {outcome} ({realized_pnl_pct:+.1f}%)."
    )


async def store_trade_setup(
    strategy: str,
    macd_signal: Optional[str],
    sma_trend: Optional[str],
    bollinger_zone: Optional[str],
    rsi_zone: Optional[str],
    realized_pnl_pct: float,
    close_reason: str,
    embeddings: VoyageEmbeddings,
) -> None:
    setup_text = build_setup_text(strategy, macd_signal, sma_trend, bollinger_zone, rsi_zone, realized_pnl_pct, close_reason)
    vector = embeddings.embed_query(setup_text)

    db_url = os.getenv("DATABASE_URL")
    conn = await asyncpg.connect(db_url)
    await register_vector(conn)
    try:
        await conn.execute(
            """
            INSERT INTO trade_setup_vectors
                (id, setup_text, strategy, macd_signal, sma_trend, bollinger_zone, rsi_zone,
                 realized_pnl_pct, close_reason, setup_embedding)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            """,
            str(uuid.uuid4()), setup_text, strategy, macd_signal, sma_trend, bollinger_zone, rsi_zone,
            realized_pnl_pct, close_reason, vector,
        )
    finally:
        await conn.close()


async def query_similar_setups(
    macd_signal: Optional[str],
    sma_trend: Optional[str],
    bollinger_zone: Optional[str],
    rsi_zone: Optional[str],
    embeddings: VoyageEmbeddings,
    top_k: int = 5,
) -> List[dict]:
    """Not yet called from the live decision path — see module docstring.
    Returns the top_k most similar historical (closed) setups, each with its
    realized outcome, for a candidate entry described by the given signals."""
    query_text = f"strategy entered on MACD={macd_signal}, EMA trend={sma_trend}, Bollinger={bollinger_zone}, RSI={rsi_zone}."
    query_embedding = embeddings.embed_query(query_text)

    db_url = os.getenv("DATABASE_URL")
    conn = await asyncpg.connect(db_url)
    await register_vector(conn)
    try:
        rows = await conn.fetch(
            """
            SELECT setup_text, realized_pnl_pct, close_reason, 1 - (setup_embedding <=> $1) AS similarity
            FROM trade_setup_vectors
            ORDER BY similarity DESC
            LIMIT $2
            """,
            query_embedding,
            top_k,
        )
        return [dict(r) for r in rows]
    finally:
        await conn.close()
