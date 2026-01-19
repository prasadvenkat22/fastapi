from abc import ABC, abstractmethod
from typing import List, Optional, Dict, Any
import os
import httpx
from .llm_integration import LLMRequest, LLMResponse, DocumentRequest, DocumentResponse

# region LLM Providers
class BaseLLM(ABC):
    @abstractmethod
    async def generate(self, request: LLMRequest) -> LLMResponse:
        pass

class OpenAILLM(BaseLLM):
    async def generate(self, request: LLMRequest) -> LLMResponse:
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
            except httpx.HTTPStatusError as e:
                error_detail = e.response.text if hasattr(e, 'response') else str(e)
                return LLMResponse(content=f"Error calling OpenAI: {e}\nDetails: {error_detail}", metadata={"source": "error"})
            except Exception as e:
                return LLMResponse(content=f"Error calling OpenAI: {e}", metadata={"source": "error"})

def llm_factory(provider: str) -> BaseLLM:
    if provider == "openai":
        return OpenAILLM()
    # Add other providers here
    else:
        raise ValueError(f"LLM provider '{provider}' not supported.")

async def generate_response(request: LLMRequest) -> LLMResponse:
    try:
        llm = llm_factory(request.llm_provider)
        return await llm.generate(request)
    except ValueError as e:
        return LLMResponse(content=str(e), metadata={"source": "error"})

# endregion

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
        full_context = "\n\n".join([u["content"] for u in uploads])
        prompt = f"Based on the following documents, please answer this question: {request.query}\n\nDocuments:\n{full_context}"
        llm_req = LLMRequest(prompt=prompt)
        llm_response = await generate_response(llm_req)
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

        context = "\n\n".join([r.content for r in results])
        prompt = f"Based on the following information, answer the question: {request.query}\n\nContext:\n{context}"
        llm_req = LLMRequest(prompt=prompt)
        llm_response = await generate_response(llm_req)
        return [
            DocumentResponse(
                id=None,
                content=llm_response.content,
                metadata={"source": "rag_with_vector_search"},
            )
        ]
    except Exception as e:
        raise RuntimeError(f"Error during RAG operation: {e}") from e