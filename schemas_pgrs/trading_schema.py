"""Pydantic schemas for the trading engine — kept in its own file, isolated
from the core e-commerce schemas in schema.py, per the trading feature's
"keep it isolated" requirement."""

from datetime import datetime
from typing import Any, Dict, Optional

from pydantic import BaseModel, ConfigDict, Field


class MarketSentimentOutput(BaseModel):
    """Structured output contract for Claude's macro-risk classification call."""
    verdict: str = Field(description="The final risk classification. Must be exactly 'GOOD' or 'BAD'.")
    confidence_score: float = Field(description="Confidence score of the analysis between 0.0 and 1.0.")
    risk_factor: str = Field(description="A one-sentence summary of the single biggest macro risk or benefit found.")


class TradingCycleResponse(BaseModel):
    execution_status: str
    macd_signal: str
    sma_trend: str
    bollinger_zone: str
    market_sentiment: str
    buy_more_count: int


class TradingLogResponse(BaseModel):
    id: str
    timestamp: Optional[datetime] = None
    execution_status: str
    macd_signal: str
    sma_trend: str
    bollinger_zone: str
    market_sentiment: str
    raw_log_payload: Dict[str, Any]

    model_config = ConfigDict(from_attributes=True)


class KillSwitchResponse(BaseModel):
    kill_switch_active: bool
