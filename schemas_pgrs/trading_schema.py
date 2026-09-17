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


class BrokerPosition(BaseModel):
    """One reconstructed spread, as orphans.py sees it.

    Distinct from OpenPositionResponse, which describes the ENGINE's own row
    and knows nothing about anything a human opened. On 2026-09-03 that
    endpoint would have reported nothing while seven structures were live.
    """
    underlying: str
    right: str                      # 'C' or 'P'
    long_strike: float
    short_strike: float
    quantity: int
    expiry: str                     # YYMMDD, as it appears in the OCC symbol
    credit: bool
    entry: float                    # from the OPENING FILLS, not cost_basis
    current_value: Optional[float] = None
    # WHY the value is what it is. A deep ITM spread marks far below its worth
    # because the short leg carries time premium; extrinsic goes to zero at
    # expiry, so a negative extrinsic is the part that comes back rather than a
    # loss. A bare return cannot tell that from a real adverse move.
    intrinsic: Optional[float] = None
    extrinsic: Optional[float] = None
    return_pct: Optional[float] = None
    peak_pct: Optional[float] = None
    minutes_since_peak: Optional[float] = None
    ceiling_value: Optional[float] = None

    # THE EXIT LADDER AS IT APPLIES TO *THIS* STRUCTURE.
    #
    # Every value below is asked of the same function the engine asks, never
    # read off a raw setting. The first version read the knobs directly and
    # went stale the moment the giveback moved from a flat percent to a share
    # of the band: it reported 40.0 on a weekly whose real threshold was 9.1
    # points, and null for the stop on a weekly that has one at -25%/15min.
    # That is the failure this model's own docstring warns about -- a field
    # naming a rule that cannot fire -- and it is section 131's lesson again:
    # a value computed twice drifts, and this one already had.
    stop_pct: Optional[float] = None        # null when the rule does not apply
    stop_confirm_minutes: Optional[float] = None
    stall_giveback_points: Optional[float] = None   # points of RETURN, not a knob
    stall_quiet_minutes: Optional[float] = None
    stall_armed: Optional[bool] = None
    stall_min_gain_pct: Optional[float] = None      # the mark must book this much
    drag_ceiling: Optional[float] = None            # dollars of intrinsic, this structure
    drag_now: Optional[float] = None                # intrinsic - mark, right now
    drag_blocks: Optional[bool] = None              # is a profitable exit refused?
    hold_until: Optional[str] = None                # no rule acts before this
    past_hold: Optional[bool] = None

    # Deprecated: the flat percent the band overrides. Kept so an existing
    # reader does not break, and populated from the real threshold rather than
    # the dead knob.
    stall_giveback_pct: Optional[float] = None

    expires_today: bool
    managed: bool
    quote_tradeable: Optional[bool] = None
    note: Optional[str] = None


class BrokerPositionsResponse(BaseModel):
    positions: list[BrokerPosition]
    count: int
    managed_underlyings: list[str]
    total_unrealized_dollars: Optional[float] = None


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
