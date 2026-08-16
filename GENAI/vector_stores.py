from abc import ABC, abstractmethod
from typing import List, Dict, Optional, Any
import os
import re
from langchain_voyageai import VoyageAIEmbeddings as LangchainVoyageAIEmbeddings
from .llm_integration import DocumentResponse

# Metadata filter keys are interpolated into the SQL query (asyncpg can't bind
# a JSONB path key as a parameter) — restrict to a safe charset to prevent injection.
_SAFE_FILTER_KEY = re.compile(r"^[a-zA-Z0-9_]+$")

# Voyage AI embedding dimension for the "voyage-4" model — used to size the pgvector column below.
# voyage-4 gives 200M free tokens per account before billing kicks in.
VOYAGE_EMBEDDING_DIM = 1024

# region Embeddings
class Embeddings(ABC):
    @abstractmethod
    def embed_query(self, text: str) -> List[float]:
        pass

    @abstractmethod
    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        pass

class VoyageEmbeddings(Embeddings):
    def __init__(self):
        self.embeddings = LangchainVoyageAIEmbeddings(
            model="voyage-4",
            voyage_api_key=os.environ.get("VOYAGE_API_KEY"),
        )

    def embed_query(self, text: str) -> List[float]:
        return self.embeddings.embed_query(text)

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        return self.embeddings.embed_documents(texts)

def embedding_factory(provider: str) -> Embeddings:
    if provider == "voyage":
        return VoyageEmbeddings()
    # Add other providers here
    else:
        raise ValueError(f"Embedding provider '{provider}' not supported.")

# endregion

class VectorDocument:
    def __init__(self, id: str, text: str, metadata: Dict[str, Any], embedding: Optional[List[float]] = None):
        self.id = id
        self.text = text
        self.metadata = metadata
        self.embedding = embedding

class BaseVectorStore(ABC):
    def __init__(self, embeddings: Embeddings):
        self.embeddings = embeddings
        
    @abstractmethod
    async def get_documents(self, query: str, top_k: int, filters: Optional[Dict[str, Any]] = None) -> List[DocumentResponse]:
        pass

    @abstractmethod
    async def add_documents(self, documents: List[VectorDocument]):
        pass

import asyncpg
import json
from pgvector.asyncpg import register_vector

class PGVectorStore(BaseVectorStore):
    def __init__(self, embeddings: Embeddings, db_url: str = os.getenv("DATABASE_URL")):
        super().__init__(embeddings)
        if not db_url:
            raise ValueError("PostgreSQL database URL not found.")
        self.db_url = db_url

    async def _init_db(self):
        conn = await asyncpg.connect(self.db_url)
        try:
            await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
            await conn.execute(f"""
                CREATE TABLE IF NOT EXISTS documents (
                    id TEXT PRIMARY KEY,
                    text TEXT,
                    metadata JSONB,
                    embedding VECTOR({VOYAGE_EMBEDDING_DIM})
                )
            """)
        finally:
            await conn.close()

    async def get_documents(self, query: str, top_k: int, filters: Optional[Dict[str, Any]] = None) -> List[DocumentResponse]:
        await self._init_db()
        conn = await asyncpg.connect(self.db_url)
        await register_vector(conn)
        try:
            query_embedding = self.embeddings.embed_query(query)
            params: List[Any] = [query_embedding]
            where_clause = ""
            if filters:
                conditions = []
                for key, value in filters.items():
                    if not _SAFE_FILTER_KEY.match(key):
                        raise ValueError(f"Invalid filter key: {key!r}")
                    params.append(str(value))
                    conditions.append(f"metadata->>'{key}' = ${len(params)}")
                where_clause = "WHERE " + " AND ".join(conditions)
            params.append(top_k)
            # Cosine similarity is used by default
            query_sql = f"""
                SELECT id, text, metadata, 1 - (embedding <=> $1) AS similarity
                FROM documents
                {where_clause}
                ORDER BY similarity DESC
                LIMIT ${len(params)}
            """
            results = await conn.fetch(query_sql, *params)
            return [DocumentResponse(id=r['id'], content=r['text'], metadata=json.loads(r['metadata']) if isinstance(r['metadata'], str) else r['metadata'], score=r['similarity']) for r in results]
        finally:
            await conn.close()

    async def add_documents(self, documents: List[VectorDocument]):
        await self._init_db()
        conn = await asyncpg.connect(self.db_url)
        await register_vector(conn)
        try:
            texts_to_embed = [doc.text for doc in documents if doc.embedding is None]
            if texts_to_embed:
                embeddings = self.embeddings.embed_documents(texts_to_embed)
                embedding_map = dict(zip(texts_to_embed, embeddings))
                for doc in documents:
                    if doc.embedding is None:
                        doc.embedding = embedding_map[doc.text]
            
            await conn.executemany(
                """
                INSERT INTO documents (id, text, metadata, embedding)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (id)
                DO UPDATE SET text = EXCLUDED.text, metadata = EXCLUDED.metadata, embedding = EXCLUDED.embedding
                """,
                [(doc.id, doc.text, json.dumps(doc.metadata), doc.embedding) for doc in documents]
            )
        finally:
            await conn.close()

def vector_store_factory(name: str = "pgvector", embedding_provider: str = "voyage") -> BaseVectorStore:
    embeddings = embedding_factory(embedding_provider)
    if name == "pgvector":
        return PGVectorStore(embeddings)
    else:
        raise ValueError(f"Vector store '{name}' not supported.")
