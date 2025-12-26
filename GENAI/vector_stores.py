from abc import ABC, abstractmethod
from typing import List, Dict, Optional, Any
import os
from langchain_openai import OpenAIEmbeddings
from langchain_community.vectorstores import FAISS
from .rag_service import DocumentResponse

class VectorDocument:
    def __init__(self, id: str, text: str, metadata: Dict[str, Any], embedding: Optional[List[float]] = None):
        self.id = id
        self.text = text
        self.metadata = metadata
        self.embedding = embedding

class BaseVectorStore(ABC):
    @abstractmethod
    async def get_documents(self, query: str, top_k: int, filters: Optional[Dict[str, Any]] = None) -> List[DocumentResponse]:
        pass

    @abstractmethod
    async def add_documents(self, documents: List[VectorDocument]):
        pass

class FAISSVectorStore(BaseVectorStore):
    def __init__(self, vs_path: str = os.getenv("GENAI_VECTORSTORE_PATH", "local_vectorstore/db_faiss")):
        if not vs_path:
            raise ValueError("FAISS vector store path not found.")
        self.vs_path = vs_path
        self.embeddings = OpenAIEmbeddings()
        self.vectorstore = None
        if os.path.exists(vs_path):
            self.vectorstore = FAISS.load_local(vs_path, self.embeddings, allow_dangerous_deserialization=True)

    async def get_documents(self, query: str, top_k: int, filters: Optional[Dict[str, Any]] = None) -> List[DocumentResponse]:
        if not self.vectorstore:
            return []
        
        docs_and_scores = self.vectorstore.similarity_search_with_score(query, k=top_k, filter=filters)
        
        results = []
        for doc, score in docs_and_scores:
            results.append(DocumentResponse(
                id=getattr(doc, "metadata", {}).get("id"),
                content=doc.page_content,
                metadata=getattr(doc, "metadata", {}),
                score=float(score)
            ))
        return results

    async def add_documents(self, documents: List[VectorDocument]):
        if not self.vectorstore:
            texts = [doc.text for doc in documents]
            metadatas = [doc.metadata for doc in documents]
            self.vectorstore = FAISS.from_texts(texts, self.embeddings, metadatas=metadatas)
        else:
            texts = [doc.text for doc in documents]
            metadatas = [doc.metadata for doc in documents]
            self.vectorstore.add_texts(texts, metadatas=metadatas)
        
        self.vectorstore.save_local(self.vs_path)

import asyncpg
from pgvector.asyncpg import register_vector

class PGVectorStore(BaseVectorStore):
    def __init__(self, db_url: str = os.getenv("DATABASE_URL")):
        if not db_url:
            raise ValueError("PostgreSQL database URL not found.")
        self.db_url = db_url
        self.embeddings = OpenAIEmbeddings()

    async def _init_db(self):
        conn = await asyncpg.connect(self.db_url)
        try:
            await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS documents (
                    id TEXT PRIMARY KEY,
                    text TEXT,
                    metadata JSONB,
                    embedding VECTOR(1536)
                )
            """)
        finally:
            await conn.close()

    async def get_documents(self, query: str, top_k: int, filters: Optional[Dict[str, Any]] = None) -> List[DocumentResponse]:
        conn = await asyncpg.connect(self.db_url)
        await register_vector(conn)
        try:
            query_embedding = self.embeddings.embed_query(query)
            # Cosine similarity is used by default
            query_sql = "SELECT id, text, metadata, 1 - (embedding <=> $1) AS similarity FROM documents ORDER BY similarity DESC LIMIT $2"
            results = await conn.fetch(query_sql, query_embedding, top_k)
            return [DocumentResponse(id=r['id'], content=r['text'], metadata=r['metadata'], score=r['similarity']) for r in results]
        finally:
            await conn.close()

    async def add_documents(self, documents: List[VectorDocument]):
        conn = await asyncpg.connect(self.db_url)
        await register_vector(conn)
        try:
            for doc in documents:
                if doc.embedding is None:
                    doc.embedding = self.embeddings.embed_query(doc.text)
            await conn.executemany(
                """
                INSERT INTO documents (id, text, metadata, embedding) 
                VALUES ($1, $2, $3, $4) 
                ON CONFLICT (id) 
                DO UPDATE SET text = EXCLUDED.text, metadata = EXCLUDED.metadata, embedding = EXCLUDED.embedding
                """,
                [(doc.id, doc.text, doc.metadata, doc.embedding) for doc in documents]
            )
        finally:
            await conn.close()

def vector_store_factory(name: str) -> BaseVectorStore:
    if name == "faiss":
        return FAISSVectorStore()
    elif name == "pgvector":
        return PGVectorStore()
    else:
        raise ValueError(f"Vector store '{name}' not supported.")
