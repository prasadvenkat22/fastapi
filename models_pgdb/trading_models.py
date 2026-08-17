import uuid

from pgvector.sqlalchemy import Vector
from sqlalchemy import Column, DateTime, Integer, String
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
