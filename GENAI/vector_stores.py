from abc import ABC, abstractmethod
from typing import List, Dict, Optional, Any
import os
from langchain_openai import OpenAIEmbeddings as LangchainOpenAIEmbeddings
from langchain_community.vectorstores import FAISS
from .llm_integration import DocumentResponse

# region Embeddings
class Embeddings(ABC):
    @abstractmethod
    def embed_query(self, text: str) -> List[float]:
        pass

    @abstractmethod
    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        pass

class OpenAIEmbeddings(Embeddings):
    def __init__(self):
        self.embeddings = LangchainOpenAIEmbeddings()

    def embed_query(self, text: str) -> List[float]:
        return self.embeddings.embed_query(text)

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        return self.embeddings.embed_documents(texts)

def embedding_factory(provider: str) -> Embeddings:
    if provider == "openai":
        return OpenAIEmbeddings()
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

class FAISSVectorStore(BaseVectorStore):
    def __init__(self, embeddings: Embeddings, vs_path: str = os.getenv("GENAI_VECTORSTORE_PATH", "local_vectorstore/db_faiss")):
        super().__init__(embeddings)
        if not vs_path:
            raise ValueError("FAISS vector store path not found.")
        self.vs_path = vs_path
        self.vectorstore = None
        if os.path.exists(vs_path):
            self.vectorstore = FAISS.load_local(vs_path, self.embeddings.embeddings, allow_dangerous_deserialization=True) # Reaching into the wrapper for now

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
        texts = [doc.text for doc in documents]
        metadatas = [doc.metadata for doc in documents]
        
        # Ensure the directory exists
        os.makedirs(os.path.dirname(self.vs_path), exist_ok=True)
        
        if not self.vectorstore:
            self.vectorstore = FAISS.from_texts(texts, self.embeddings.embeddings, metadatas=metadatas) # Reaching into the wrapper for now
        else:
            self.vectorstore.add_texts(texts, metadatas=metadatas)
        
        self.vectorstore.save_local(self.vs_path)

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
            return [DocumentResponse(id=r['id'], content=r['text'], metadata=json.loads(r['metadata']) if isinstance(r['metadata'], str) else r['metadata'], score=r['similarity']) for r in results]
        finally:
            await conn.close()

    async def add_documents(self, documents: List[VectorDocument]):
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

def vector_store_factory(name: str, embedding_provider: str = "openai") -> BaseVectorStore:
    embeddings = embedding_factory(embedding_provider)
    if name == "faiss":
        return FAISSVectorStore(embeddings)
    elif name == "pgvector":
        return PGVectorStore(embeddings)
    else:
        raise ValueError(f"Vector store '{name}' not supported.")
