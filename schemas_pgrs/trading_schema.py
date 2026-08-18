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
    rsi_zone: str
    market_sentiment: str
    buy_more_count: int


class TradingLogResponse(BaseModel):
    id: str
    timestamp: Optional[datetime] = None
    execution_status: str
    macd_signal: str
    sma_trend: str
    bollinger_zone: str
    rsi_zone: Optional[str] = None
    market_sentiment: str
    raw_log_payload: Dict[str, Any]

    model_config = ConfigDict(from_attributes=True)


class KillSwitchResponse(BaseModel):
    kill_switch_active: bool


class SchedulerStatusResponse(BaseModel):
    scheduler_running: bool
    interval_seconds: int


class OpenPositionResponse(BaseModel):
    open: bool
    strategy: Optional[str] = None
    underlying: Optional[str] = None
    quantity: Optional[int] = None
    long_strike: Optional[float] = None
    short_strike: Optional[float] = None
    entry_net_debit: Optional[float] = None
    current_spot: Optional[float] = None
    estimated_current_value: Optional[float] = None
    unrealized_pnl_pct: Optional[float] = None
    unrealized_pnl_dollars: Optional[float] = None
    opened_at: Optional[datetime] = None


class TradeHistoryEntry(BaseModel):
    strategy: str
    underlying: str
    quantity: int
    long_strike: float
    short_strike: float
    entry_net_debit: float
    exit_net_value: float
    realized_pnl_dollars: float
    realized_pnl_pct: float
    close_reason: str
    opened_at: Optional[datetime] = None
    closed_at: Optional[datetime] = None
    entry_macd_signal: Optional[str] = None
    entry_sma_trend: Optional[str] = None
    entry_bollinger_zone: Optional[str] = None
    entry_rsi_zone: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class TradeHistoryResponse(BaseModel):
    total_realized_pnl_dollars: float
    trade_count: int
    trades: list[TradeHistoryEntry]


class PlaybookStat(BaseModel):
    """Realized performance for one named entry strategy."""
    playbook: str
    trades: int
    wins: int
    losses: int
    win_rate_pct: float
    total_pnl_dollars: float
    avg_pnl_pct: float
    best_pct: float
    worst_pct: float
    close_reasons: dict[str, int]
    active: bool          # still in playbook.WINDOWS, i.e. still being traded
    window: Optional[str] = None
    placement: Optional[str] = None


class PlaybookPerformanceResponse(BaseModel):
    """Per-strategy scoreboard. Strategies with no closed trades still appear
    with zeroes so an untested one is visibly untested rather than absent."""
    stats: list[PlaybookStat]
    unattributed_trades: int   # closed before playbook tracking existed


class TradingStatusResponse(BaseModel):
    """One-call dashboard combining kill switch, scheduler, open position,
    and realized P&L — everything /trading/position, /trading/history, and
    /trading/scheduler/status report individually, in a single response."""

    kill_switch_active: bool
    scheduler_running: bool
    scheduler_interval_seconds: int
    position: OpenPositionResponse
    total_realized_pnl_dollars: float
    closed_trade_count: int
    last_execution_status: Optional[str] = None
    last_cycle_at: Optional[datetime] = None
