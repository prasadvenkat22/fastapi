"""pgvector-backed storage/lookup for scraped macro headlines — same asyncpg +
cosine-distance pattern as GENAI/vector_stores.py's PGVectorStore, applied to
the trading domain's own market_news_vectors table."""

import json
import os
import uuid
from typing import List

import asyncpg
from pgvector.asyncpg import register_vector

from GENAI.vector_stores import VoyageEmbeddings

EMBEDDING_DIM = 1024  # matches voyage-4, same as GENAI's documents table


async def _init_db(conn: asyncpg.Connection) -> None:
    await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    await conn.execute(f"""
        CREATE TABLE IF NOT EXISTS market_news_vectors (
            id TEXT PRIMARY KEY,
            headline_text TEXT NOT NULL,
            publication_date TIMESTAMPTZ DEFAULT now(),
            text_embedding VECTOR({EMBEDDING_DIM})
        )
    """)


def _source_for(headline: str) -> "str | None":
    """Which feed this headline arrived on, if the scrape recorded it."""
    try:
        from .nodes import _LAST_SOURCES
        return _LAST_SOURCES.get(headline)
    except Exception:
        return None


async def store_headlines(headlines: List[str], embeddings: VoyageEmbeddings) -> None:
    """Embed and store any headline not already held.

    The dedupe is not tidiness. This runs once a cycle, the scrape returns
    much the same front page minute after minute, and every repeat was being
    re-embedded and re-inserted: 7,200 rows covering 605 distinct headlines
    over four sessions, or about twelve embedding calls for every one that
    carried new information.

    It also degraded the thing the store exists for. query_similar_headlines
    asks for the three nearest past headlines, and with each headline stored
    a dozen times the three nearest were usually three copies of one
    headline -- so the model was handed the same sentence three times and
    told it was context.
    """
    if not headlines:
        return

    db_url = os.getenv("DATABASE_URL")
    conn = await asyncpg.connect(db_url)
    await register_vector(conn)
    try:
        await _init_db(conn)
        known = await conn.fetch(
            "SELECT headline_text FROM market_news_vectors WHERE headline_text = ANY($1::text[])",
            list(set(headlines)),
        )
        seen = {r["headline_text"] for r in known}
        fresh = [h for h in dict.fromkeys(headlines) if h not in seen]
        if not fresh:
            return

        vectors = embeddings.embed_documents(fresh)
        await conn.executemany(
            """
            INSERT INTO market_news_vectors (id, headline_text, text_embedding, source)
            VALUES ($1, $2, $3, $4)
            """,
            # source is looked up per headline from the scrape that produced
            # it. Absent before 2026-09-06, which left no way to weight a wire
            # above a blog -- see nodes._LAST_SOURCES.
            [(str(uuid.uuid4()), headline, vector, _source_for(headline))
             for headline, vector in zip(fresh, vectors)],
        )
    finally:
        await conn.close()


async def query_similar_headlines(headlines: List[str], embeddings: VoyageEmbeddings, top_k: int = 3) -> List[str]:
    """Embeds today's combined headline text as one query vector and returns the
    top_k most similar historical headlines already stored, for extra context
    in the sentiment prompt."""
    if not headlines:
        return []

    query_text = "\n".join(headlines)
    query_embedding = embeddings.embed_query(query_text)

    db_url = os.getenv("DATABASE_URL")
    conn = await asyncpg.connect(db_url)
    await register_vector(conn)
    try:
        await _init_db(conn)
        rows = await conn.fetch(
            """
            SELECT headline_text, 1 - (text_embedding <=> $1) AS similarity
            FROM market_news_vectors
            ORDER BY similarity DESC
            LIMIT $2
            """,
            query_embedding,
            top_k,
        )
        return [r["headline_text"] for r in rows]
    finally:
        await conn.close()
