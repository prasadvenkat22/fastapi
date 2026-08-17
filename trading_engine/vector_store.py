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


async def store_headlines(headlines: List[str], embeddings: VoyageEmbeddings) -> None:
    if not headlines:
        return

    vectors = embeddings.embed_documents(headlines)
    db_url = os.getenv("DATABASE_URL")
    conn = await asyncpg.connect(db_url)
    await register_vector(conn)
    try:
        await _init_db(conn)
        await conn.executemany(
            """
            INSERT INTO market_news_vectors (id, headline_text, text_embedding)
            VALUES ($1, $2, $3)
            """,
            [(str(uuid.uuid4()), headline, vector) for headline, vector in zip(headlines, vectors)],
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
