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
