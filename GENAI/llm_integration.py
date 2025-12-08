from typing import List, Optional, Dict, Any
import os
import json # Re-adding this as it was used in call_openai_chat fallback previously.
from pydantic import BaseModel

from openai import AsyncOpenAI # Moved this after BaseModel import, usually third-party imports come after standard library and project-specific imports, and pydantic is quite core here.

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
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY environment variable is not set")

    # Configure openai SDK using the new v1.x client
    base_url = os.getenv("OPENAI_API_BASE")
    client = AsyncOpenAI(api_key=api_key, base_url=base_url)

    try:
        # Use the async ChatCompletion interface
        resp = await client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=temperature,
        )

        content = None
        if resp.choices and len(resp.choices) > 0:
            choice = resp.choices[0]
            if choice.message and choice.message.content:
                content = choice.message.content

        if content is not None:
            return content

    except Exception as e:
        # Last resort: return error message
        return f"Error calling OpenAI: {str(e)}"

    # Fallback for unexpected response structure
    try:
        return resp.model_dump_json()
    except Exception:
        return str(resp)


async def run_rag(request: DocumentRequest, uploads: Optional[List[Dict[str, Any]]] = None) -> List[DocumentResponse]:
    # Try vector retrieval when LangChain + vectorstore present
    if _langchain_available():
        try:
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
            pass

    # If uploads (uploaded documents) are provided, prefer calling the LLM with the uploaded context
    if uploads:
        try:
            # concatenate uploaded contents into a context block
            ctx_parts = []
            for u in uploads:
                c = u.get("content") if isinstance(u, dict) else str(u)
                meta = u.get("metadata") if isinstance(u, dict) else {}
                header = f"[Uploaded doc: {meta.get('filename')}]" if isinstance(meta, dict) and meta.get("filename") else "[Uploaded doc]"
                ctx_parts.append(header + "\n" + c)

            prompt = "\n\n---\n\n".join(ctx_parts) + "\n\nUser query: " + request.query
            output = await call_openai_chat(prompt)
            return [DocumentResponse(id=None, content=output, metadata={"source": "uploaded"}, score=None)]
        except Exception as e:
            return [DocumentResponse(id=None, content=f"LLM error with uploads: {e}", metadata={}, score=None)]

    # Fallback to LLM if API key available
    api_key = os.getenv("OPENAI_API_KEY")
    if api_key:
        try:
            output = await call_openai_chat(request.query)
            return [DocumentResponse(id=None, content=output, metadata={"source": "openai"}, score=None)]
        except Exception as e:
            return [DocumentResponse(id=None, content=f"LLM error: {e}", metadata={}, score=None)]

    # Final fallback: echo
    return [DocumentResponse(id=None, content=f"Echo: {request.query}", metadata={}, score=None)]
