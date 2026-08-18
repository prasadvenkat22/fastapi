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
    # Named time-window strategy that opened this position (see
    # trading_engine/playbook.py). Carried through to TradeHistory on close so
    # realized P&L can be attributed per strategy rather than pooled.
    playbook = Column(String, nullable=True, index=True)
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
    close_reason = Column(String, nullable=False)  # TAKE_PROFIT / STOP_LOSS / RISK_OFF / FORCE_CLOSE
    playbook = Column(String, nullable=True, index=True)  # which named strategy opened it
    opened_at = Column(DateTime(timezone=True), nullable=True)
    closed_at = Column(DateTime(timezone=True), default=func.now())
    entry_macd_signal = Column(String, nullable=True)
    entry_sma_trend = Column(String, nullable=True)
    entry_bollinger_zone = Column(String, nullable=True)
    entry_rsi_zone = Column(String, nullable=True)


class BreadthReading(Base):
    """One cycle's market-breadth snapshot, kept so breadth can be read as a
    trend rather than a single instant.

    VIX doesn't need this — its intraday move comes free with the 1-minute
    bar series we already fetch. Breadth arrives from Tradier's quotes
    endpoint as a bare snapshot with no history attached, so the only way to
    know whether it is improving or collapsing is to write each reading down
    and compare against earlier ones from the same session."""

    __tablename__ = "trading_breadth_readings"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    addq = Column(Float, nullable=False)          # advancers - decliners
    advancers = Column(Integer, nullable=False)
    decliners = Column(Integer, nullable=False)
    unchanged = Column(Integer, nullable=False)
    basket_size = Column(Integer, nullable=False)
    # addq normalised to [-1, 1] by basket size, so thresholds stay valid if
    # NASDAQ_BREADTH_BASKET is ever resized.
    net_ratio = Column(Float, nullable=False)
    # server_default, not default: main.py's startup create_all() can win the
    # race against alembic and create this table from the model, and a
    # client-side default would leave the column with no database default at
    # all — silently drifting from what the migration declares.
    recorded_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)


class MacroCache(Base):
    """Last macro read, shared across processes.

    This used to be a module-level global, which worked only because the
    in-app scheduler kept one process alive between cycles. Under cron every
    run is a fresh process, so the cache was always empty and each cycle
    re-scraped three RSS feeds, re-embedded via Voyage and re-called Claude —
    roughly 390 Claude and 780 Voyage calls a session, against a Voyage free
    tier of 3 requests per minute.

    One row, upserted. The verdict is cheap to store and expensive to derive."""

    __tablename__ = "trading_macro_cache"
    id = Column(Integer, primary_key=True, default=1)
    verdict = Column(String, nullable=False)
    confidence = Column(Float, nullable=True)
    risk_factor = Column(String, nullable=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)


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
