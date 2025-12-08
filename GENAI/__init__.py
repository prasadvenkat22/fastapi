"""GENAI package: lightweight RAG + LangChain bridge.

This package provides a small abstraction to run retrieval-augmented-generation
workflows and expose them via FastAPI. It intentionally keeps imports lazy so
the app can run even when LangChain or vectorstore dependencies are not
installed; in that case the service falls back to a clear stub implementation.
"""

__all__ = ["rag_service", "router"]
