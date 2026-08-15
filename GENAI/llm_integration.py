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
    llm_provider: str = "anthropic"
    llm_model: str = "claude-opus-5"
    max_tokens: int = 800


class LLMResponse(BaseModel):
    content: str
    metadata: Dict = {}

class DocumentResponse(BaseModel):
    id: Optional[str]
    content: str
    metadata: Dict = {}
    score: Optional[float] = None

