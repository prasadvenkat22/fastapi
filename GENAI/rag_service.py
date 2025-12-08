from typing import List, Optional, Dict
import os
import json
from pydantic import BaseModel

import httpx


class DocumentRequest(BaseModel):
    query: str
    top_k: int = 4
    filters: Optional[Dict] = None


class DocumentResponse(BaseModel):
    id: Optional[str]
    content: str
    metadata: Dict = {}
    score: Optional[float] = None


def _langchain_available() -> bool:
    try:
        import langchain  # noqa: F401
        return True
    except Exception:
        return False


async def call_openai_chat(prompt: str, model: str = "gpt-4o-mini", temperature: float = 0.0) -> str:
    """Call OpenAI Chat Completions (async) using httpx.

    Respects `OPENAI_API_KEY` and optional `OPENAI_API_BASE` (for Azure or custom base).
    Returns the assistant text content.
    """
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY environment variable is not set")

    base = os.getenv("OPENAI_API_BASE", "https://api.openai.com")
    # default Chat Completions endpoint
    url = f"{base.rstrip('/')}/v1/chat/completions"

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": 800,
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(url, headers=headers, json=payload)
        resp.raise_for_status()
        data = resp.json()

    # extract assistant message
    try:
        # OpenAI chat completion shape
        return data["choices"][0]["message"]["content"]
    except Exception:
        # fallback: return raw json string
        return json.dumps(data)


async def run_rag(request: DocumentRequest) -> List[DocumentResponse]:
    """Run a simple RAG retrieval for `request.query`.

    Behavior:
    - If LangChain and a vectorstore are available, run a retrieval and return matches.
    - Else, if `OPENAI_API_KEY` is set, call the LLM with the query and return a single DocumentResponse.
    - Otherwise return an echo fallback.
    """
    # If langchain + a saved vectorstore are available, do retrieval
    if _langchain_available():
        try:
            # Lazy imports to avoid import-time failures when dependencies are missing
            from langchain.embeddings import OpenAIEmbeddings
            from langchain.vectorstores import FAISS

            vs_path = os.getenv("GENAI_VECTORSTORE_PATH")
            if vs_path and os.path.exists(vs_path):
                embeddings = OpenAIEmbeddings()
                vectorstore = FAISS.load_local(vs_path, embeddings)
                docs_and_scores = vectorstore.similarity_search_with_score(request.query, k=request.top_k)
                results: List[DocumentResponse] = []
                for doc, score in docs_and_scores:
                    results.append(DocumentResponse(id=getattr(doc, "metadata", {}).get("id"),
                                                    content=doc.page_content,
                                                    metadata=getattr(doc, "metadata", {}),
                                                    score=float(score)))
                if results:
                    return results
        except Exception:
            # If any langchain step fails, fall through to LLM or echo fallback
            pass

    # Try calling LLM if API key is present
    api_key = os.getenv("OPENAI_API_KEY")
    if api_key:
        try:
            prompt = request.query
            llm_output = await call_openai_chat(prompt)
            return [DocumentResponse(id=None, content=llm_output, metadata={"source": "openai"}, score=None)]
        except Exception as e:
            return [DocumentResponse(id=None, content=f"LLM error: {e}", metadata={}, score=None)]

    # final safe fallback: structured echo
    return [DocumentResponse(id=None, content=f"Echo: {request.query}", metadata={}, score=None)]
from typing import List, Optional, Dict
import os
import json
from pydantic import BaseModel

import httpx


class DocumentRequest(BaseModel):
    query: str
    top_k: int = 4
    filters: Optional[Dict] = None


class DocumentResponse(BaseModel):
    id: Optional[str]
    content: str
    metadata: Dict = {}
    score: Optional[float] = None


def _langchain_available() -> bool:
    try:
        import langchain  # noqa: F401
        return True
    except Exception:
        return False


async def call_openai_chat(prompt: str, model: str = "gpt-4o-mini", temperature: float = 0.0) -> str:
    """Call OpenAI Chat Completions (async) using httpx.

    Respects `OPENAI_API_KEY` and optional `OPENAI_API_BASE` (for Azure or custom base).
    Returns the assistant text content.
    """
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY environment variable is not set")

    base = os.getenv("OPENAI_API_BASE", "https://api.openai.com")
    # default Chat Completions endpoint
    url = f"{base.rstrip('/')}/v1/chat/completions"

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": 800,
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(url, headers=headers, json=payload)
        resp.raise_for_status()
        data = resp.json()

    # extract assistant message
    try:
        from typing import List, Optional, Dict
        import os
        import json
        from pydantic import BaseModel

        import httpx


        class DocumentRequest(BaseModel):
            query: str
            top_k: int = 4
            filters: Optional[Dict] = None


        class DocumentResponse(BaseModel):
            id: Optional[str]
            content: str
            metadata: Dict = {}
            score: Optional[float] = None


        def _langchain_available() -> bool:
            try:
                import langchain  # noqa: F401
                return True
            except Exception:
                return False


        async def call_openai_chat(prompt: str, model: str = "gpt-4o-mini", temperature: float = 0.0) -> str:
            """Call OpenAI Chat Completions (async) using httpx.

            Respects `OPENAI_API_KEY` and optional `OPENAI_API_BASE` (for Azure or custom base).
            Returns the assistant text content.
            """
            api_key = os.getenv("OPENAI_API_KEY")
            if not api_key:
                raise RuntimeError("OPENAI_API_KEY environment variable is not set")

            base = os.getenv("OPENAI_API_BASE", "https://api.openai.com")
            # default Chat Completions endpoint
            url = f"{base.rstrip('/')}/v1/chat/completions"

            headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

            payload = {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": temperature,
                "max_tokens": 800,
            }

            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(url, headers=headers, json=payload)
                resp.raise_for_status()
                data = resp.json()

            # extract assistant message
            try:
                # OpenAI chat completion shape
                return data["choices"][0]["message"]["content"]
            except Exception:
                # fallback: return raw json string
                return json.dumps(data)


        async def run_rag(request: DocumentRequest) -> List[DocumentResponse]:
            """Run a simple RAG retrieval for `request.query`.

            Behavior:
            - If LangChain and a vectorstore are available, run a retrieval and return matches.
            - Else, if `OPENAI_API_KEY` is set, call the LLM with the query and return a single DocumentResponse.
            - Otherwise return an echo fallback.
            """
            # If langchain + a saved vectorstore are available, do retrieval
            if _langchain_available():
                try:
                    # Lazy imports to avoid import-time failures when dependencies are missing
                    from langchain.embeddings import OpenAIEmbeddings
                    from langchain.vectorstores import FAISS

                    vs_path = os.getenv("GENAI_VECTORSTORE_PATH")
                    if vs_path and os.path.exists(vs_path):
                        embeddings = OpenAIEmbeddings()
                        vectorstore = FAISS.load_local(vs_path, embeddings)
                        docs_and_scores = vectorstore.similarity_search_with_score(request.query, k=request.top_k)
                        results: List[DocumentResponse] = []
                        for doc, score in docs_and_scores:
                            results.append(DocumentResponse(id=getattr(doc, "metadata", {}).get("id"),
                                                            content=doc.page_content,
                                                            metadata=getattr(doc, "metadata", {}),
                                                            score=float(score)))
                        if results:
                            return results
                except Exception:
                    # If any langchain step fails, fall through to LLM or echo fallback
                    pass

            # Try calling LLM if API key is present
            api_key = os.getenv("OPENAI_API_KEY")
            if api_key:
                try:
                    prompt = request.query
                    llm_output = await call_openai_chat(prompt)
                    return [DocumentResponse(id=None, content=llm_output, metadata={"source": "openai"}, score=None)]
                except Exception as e:
                    return [DocumentResponse(id=None, content=f"LLM error: {e}", metadata={}, score=None)]

            # final safe fallback: structured echo
            return [DocumentResponse(id=None, content=f"Echo: {request.query}", metadata={}, score=None)]
        async with httpx.AsyncClient(timeout=30.0) as client:
