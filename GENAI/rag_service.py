from typing import List, Optional, Dict, Any
import os
import json
from pydantic import BaseModel
import httpx

from .llm_integration import LLMRequest, LLMResponse, DocumentRequest


class DocumentResponse(BaseModel):
    id: Optional[str]
    content: str
    metadata: Dict = {}
    score: Optional[float] = None


async def call_llm(request: LLMRequest) -> LLMResponse:
    if request.llm_provider == "openai":
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY environment variable is not set")

        base = os.getenv("OPENAI_API_BASE", "https://api.openai.com")
        url = f"{base.rstrip('/')}/v1/chat/completions"
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        payload = {
            "model": request.llm_model,
            "messages": [{"role": "user", "content": request.prompt}],
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                resp = await client.post(url, headers=headers, json=payload)
                resp.raise_for_status()
                data = resp.json()
                content = data["choices"][0]["message"]["content"]
                return LLMResponse(content=content, metadata={"source": "openai"})
            except Exception as e:
                return LLMResponse(content=f"Error calling OpenAI: {e}", metadata={"source": "error"})
    else:
        return LLMResponse(content=f"LLM provider {request.llm_provider} not supported.", metadata={"source": "error"})


async def run_rag(
    request: DocumentRequest,
    vector_store: "BaseVectorStore",
    uploads: Optional[List[Dict[str, Any]]] = None,
) -> List[DocumentResponse]:
    """Run a RAG operation.

    - If `uploads` are provided, they are sent to the LLM for summarization/Q&A.
    - Otherwise, the `request.query` is used to search the `vector_store`.
    - A final LLM call is made with the context to generate a response.
    """
    if uploads:
        # Case 1: Uploaded documents are passed for Q&A or summarization
        # Create a single prompt from all uploaded content
        full_context = "\n\n".join([u["content"] for u in uploads])
        prompt = f"Based on the following documents, please answer this question: {request.query}\n\nDocuments:\n{full_context}"
        llm_req = LLMRequest(prompt=prompt)
        llm_response = await call_llm(llm_req)
        return [
            DocumentResponse(
                id=None,
                content=llm_response.content,
                metadata={"source": "llm_with_uploads"},
            )
        ]

    # Case 2: No uploads, perform a vector search
    try:
        results = await vector_store.get_documents(
            query=request.query, top_k=request.top_k, filters=request.filters
        )
        if not results:
            return [DocumentResponse(content="No results found in vector store.")]

        # Create context from vector search results
        context = "\n\n".join([r.content for r in results])
        prompt = f"Based on the following information, answer the question: {request.query}\n\nContext:\n{context}"
        llm_req = LLMRequest(prompt=prompt)
        llm_response = await call_llm(llm_req)
        return [
            DocumentResponse(
                id=None,
                content=llm_response.content,
                metadata={"source": "rag_with_vector_search"},
            )
        ]
    except Exception as e:
        raise RuntimeError(f"Error during RAG operation: {e}") from e