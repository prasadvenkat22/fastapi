from typing import Optional, Dict
from pydantic import BaseModel


# Request/response models
class DocumentRequest(BaseModel):
    query: str
    top_k: int = 4
    filters: Optional[Dict] = None
    embedding_provider: str = "voyage"


class LLMRequest(BaseModel):
    prompt: str
    llm_provider: str = "gemini"
    llm_model: str = "gemini-3.1-flash-lite"
    max_tokens: int = 800


class LLMResponse(BaseModel):
    content: str
    metadata: Dict = {}

class DocumentResponse(BaseModel):
    id: Optional[str]
    content: str
    metadata: Dict = {}
    score: Optional[float] = None

