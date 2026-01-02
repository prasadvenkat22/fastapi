from typing import Optional, Dict
from pydantic import BaseModel


# Request/response models
class DocumentRequest(BaseModel):
    query: str
    top_k: int = 4
    filters: Optional[Dict] = None
    embedding_provider: str = "openai"


class LLMRequest(BaseModel):
    prompt: str
    llm_provider: str = "openai"
    llm_model: str = "gpt-4o-mini"
    temperature: float = 0.0
    max_tokens: int = 800


class LLMResponse(BaseModel):
    content: str
    metadata: Dict = {}

class DocumentResponse(BaseModel):
    id: Optional[str]
    content: str
    metadata: Dict = {}
    score: Optional[float] = None

