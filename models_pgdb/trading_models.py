import uuid

from pgvector.sqlalchemy import Vector
from sqlalchemy import Column, DateTime, Float, Integer, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.sql import func

from config.db_pgrs import Base

EMBEDDING_DIM = 1024  # voyage-4


class TradingLog(Base):
    __tablename__ = "trading_logs"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    timestamp = Column(DateTime(timezone=True), default=func.now())
    execution_status = Column(String, nullable=False)
    macd_signal = Column(String, nullable=True)
    sma_trend = Column(String, nullable=True)
    bollinger_zone = Column(String, nullable=True)
    rsi_zone = Column(String, nullable=True)
    market_sentiment = Column(String, nullable=True)
    raw_log_payload = Column(JSONB, nullable=True)


class MarketNewsVector(Base):
    __tablename__ = "market_news_vectors"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    headline_text = Column(String, nullable=False)
    publication_date = Column(DateTime(timezone=True), default=func.now())
    text_embedding = Column(Vector(EMBEDDING_DIM), nullable=True)


class OpenPosition(Base):
    """The currently open mock spread, if any — at most one row is expected
    to exist at a time (the engine trades one QQQ spread position). Persists
    across /trading/run-daily-cycle calls so the take-profit/stop-loss/
    force-close rules have something real to check on each cycle instead of
    the engine forgetting its position between calls."""

    __tablename__ = "trading_open_positions"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    strategy = Column(String, nullable=False)  # BULL_CALL_SPREAD or BEAR_PUT_SPREAD
    underlying = Column(String, nullable=False)
    quantity = Column(Integer, nullable=False)
    long_strike = Column(Float, nullable=False)
    short_strike = Column(Float, nullable=False)
    entry_net_debit = Column(Float, nullable=False)
    opened_at = Column(DateTime(timezone=True), default=func.now())
    # Signal readings at entry — carried through to TradeHistory on close so
    # the setup that triggered this trade isn't lost.
    entry_macd_signal = Column(String, nullable=True)
    entry_sma_trend = Column(String, nullable=True)
    entry_bollinger_zone = Column(String, nullable=True)
    entry_rsi_zone = Column(String, nullable=True)


class TradeHistory(Base):
    """Realized P&L record, written whenever an open position is fully
    closed (take-profit, stop-loss, or the 0DTE force-close cutoff) — the
    OpenPosition row for a closed trade gets deleted, so without this the
    result of that trade would just disappear."""

    __tablename__ = "trading_history"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    strategy = Column(String, nullable=False)
    underlying = Column(String, nullable=False)
    quantity = Column(Integer, nullable=False)
    long_strike = Column(Float, nullable=False)
    short_strike = Column(Float, nullable=False)
    entry_net_debit = Column(Float, nullable=False)
    exit_net_value = Column(Float, nullable=False)
    realized_pnl_dollars = Column(Float, nullable=False)
    realized_pnl_pct = Column(Float, nullable=False)
    close_reason = Column(String, nullable=False)  # TAKE_PROFIT / STOP_LOSS / FORCE_CLOSE
    opened_at = Column(DateTime(timezone=True), nullable=True)
    closed_at = Column(DateTime(timezone=True), default=func.now())
    entry_macd_signal = Column(String, nullable=True)
    entry_sma_trend = Column(String, nullable=True)
    entry_bollinger_zone = Column(String, nullable=True)
    entry_rsi_zone = Column(String, nullable=True)


class TradeSetupVector(Base):
    """Embedded record of a closed trade's entry setup + outcome, written
    whenever a TradeHistory row is created. Pure logging for now -- there's
    no meaningful trade history yet to learn from (entries are rare by
    design), so this isn't wired into any live decision. Once enough real
    closed trades accumulate, query_similar_setups() in
    trading_engine/setup_vector_store.py is ready to back a similarity-based
    veto gate as a follow-up."""

    __tablename__ = "trade_setup_vectors"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    setup_text = Column(String, nullable=False)
    strategy = Column(String, nullable=False)
    macd_signal = Column(String, nullable=True)
    sma_trend = Column(String, nullable=True)
    bollinger_zone = Column(String, nullable=True)
    rsi_zone = Column(String, nullable=True)
    realized_pnl_pct = Column(Float, nullable=False)
    close_reason = Column(String, nullable=False)
    closed_at = Column(DateTime(timezone=True), default=func.now())
    setup_embedding = Column(Vector(EMBEDDING_DIM), nullable=True)
