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
    # Best return this position has shown, so a profit ratchet can measure
    # retracement from the peak rather than only from entry.
    peak_return_pct = Column(Float, nullable=True, default=0.0)
    # WHEN that peak was set. peak_return_pct says how good a position
    # has been and nothing about when, so a giveback rule cannot tell a
    # dip inside a climb from the end of one. The stalled-peak exit asks
    # whether a NEW high has been made recently, and needs this.
    peak_at = Column(DateTime(timezone=True), nullable=True)
    # Time-sliced entry plan. NULL means the position was opened in one order,
    # which is the default and was the only behaviour before 2026-08-25.
    #
    # Two columns because one cannot be derived from the other safely:
    # entry_tranche_qty is the slice size, frozen at entry so later tranches
    # do not drift as equity moves, and entry_slices_remaining counts down.
    # Deriving either from `quantity` breaks the moment anything else changes
    # it. See TRADING_ENTRY_SLICES in nodes.py for the measurement.
    entry_tranche_qty = Column(Integer, nullable=True)
    entry_slices_remaining = Column(Integer, nullable=True)
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


class MacroReading(Base):
    """VIX and 10-year yield as seen each cycle.

    Breadth was already persisted; these were not, so the values three of the
    five macro gates fire on left no trace. Answering "did yields spike the
    day we lost money" meant re-fetching from yfinance and hoping the vendor
    still had the history -- which is not a record.

    Cheap to store, and it makes threshold questions answerable from our own
    data instead of a guess."""

    __tablename__ = "trading_macro_readings"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    vix_level = Column(Float, nullable=False)
    vix_session_open = Column(Float, nullable=False)
    vix_change_pct = Column(Float, nullable=False)
    tnx_level = Column(Float, nullable=False)
    tnx_session_open = Column(Float, nullable=False)
    tnx_change_bps = Column(Float, nullable=False)
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


class WeeklyShadow(Base):
    """A weekly call credit spread the engine marks but does not trade.

    Stateful, unlike the shadow condor, and necessarily so: a weekly position
    spans days and its entry credit comes from Friday's option chain, which
    cannot be refetched afterwards. Reconstructing it from bars the way the
    condor does is impossible, so the entry has to be persisted at the moment
    it is observed.

    Two outcomes are recorded rather than one. `target_hit_at` is when the
    spread first reached the take-profit share of its credit -- the rule that
    books it Monday morning -- while `expiry_value` is what holding to
    expiration would have paid. Keeping both is the only way to learn whether
    booking early beats sitting still, which is the actual open question.
    """

    __tablename__ = "weekly_shadow"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    opened_at = Column(DateTime(timezone=True), default=func.now())
    # Which underlying. Defaulted to QQQ because every row written before
    # 2026-08-27 is one, and the column exists so the single-name book can be
    # compared name by name -- the question being not "does a weekly condor
    # work" but "on which names does implied exceed realised".
    symbol = Column(String, nullable=False, default="QQQ", index=True)
    expiration = Column(String, nullable=False)        # YYYY-MM-DD
    strategy = Column(String, nullable=False)          # CALL_CREDIT_SPREAD
    # Call side: short/long. Put side: put_short/put_long. A CALL variant
    # leaves the puts null, a PUT variant leaves the calls null, and a CONDOR
    # carries all four -- one table for three structures, because the only
    # thing that differs between them is which legs exist.
    short_strike = Column(Float, nullable=True)
    long_strike = Column(Float, nullable=True)
    put_short_strike = Column(Float, nullable=True)
    put_long_strike = Column(Float, nullable=True)
    width = Column(Float, nullable=False)

    spot_at_entry = Column(Float, nullable=False)
    short_delta = Column(Float, nullable=True)
    short_iv = Column(Float, nullable=True)
    entry_credit_mid = Column(Float, nullable=False)
    entry_credit_natural = Column(Float, nullable=False)   # what a fill would pay
    entry_spread_width = Column(Float, nullable=True)      # two-leg bid-ask to cross

    # Marked every cycle while the contract is alive.
    last_marked_at = Column(DateTime(timezone=True), nullable=True)
    last_value_mid = Column(Float, nullable=True)
    # Each side priced separately, so "close the winner and hold the loser"
    # becomes a question the record can answer instead of one to argue about.
    # Without these the condor is a single number and legging out is invisible.
    last_call_side = Column(Float, nullable=True)
    last_put_side = Column(Float, nullable=True)
    call_side_at_entry = Column(Float, nullable=True)
    put_side_at_entry = Column(Float, nullable=True)
    last_return_pct = Column(Float, nullable=True)
    peak_return_pct = Column(Float, nullable=True, default=0.0)
    worst_return_pct = Column(Float, nullable=True, default=0.0)
    breached = Column(String, nullable=True)               # first timestamp spot >= short

    # The two outcomes.
    target_hit_at = Column(DateTime(timezone=True), nullable=True)
    target_return_pct = Column(Float, nullable=True)
    expiry_value = Column(Float, nullable=True)
    expiry_return_pct = Column(Float, nullable=True)
    notes = Column(String, nullable=True)

    # Market state this row was opened into, on DAILY bars (see
    # trading_engine/weekly_signals.py). Recorded, never acted on: the book is
    # still shadow-only and every structure is still opened on every symbol.
    # These exist so that "should the weekly book be gated on trend, Bollinger
    # position or realised-against-implied vol" becomes a question the record
    # can answer in ten Fridays instead of an argument. Weekly option prices
    # have no historical feed, so there is no other route to the answer.
    #
    # Labels AND numbers: the label is what a gate would read, the number is
    # what lets a different threshold be tested without re-collecting.
    sig_trend = Column(String, nullable=True)          # ABOVE_BOTH/BELOW_BOTH/MIXED
    sig_ema_cross = Column(String, nullable=True)      # EMA9 vs EMA20
    sig_bb_zone = Column(String, nullable=True)        # UPPER_BAND/LOWER_BAND/NORMAL
    sig_bb_sd = Column(Float, nullable=True)
    sig_rsi14 = Column(Float, nullable=True)
    sig_sma20 = Column(Float, nullable=True)
    sig_sma50 = Column(Float, nullable=True)
    sig_ema20 = Column(Float, nullable=True)
    sig_move_5d_pct = Column(Float, nullable=True)
    sig_rv20 = Column(Float, nullable=True)            # annualised realised vol
    # Above 1.0 the name realises more than it charges -- the wrong side of the
    # variance risk premium, and the reading that decides whether any of the
    # rest matters. Per VARIANT, since the call and the put quote at different
    # vols and one ratio per symbol would hide the skew.
    sig_rv_iv_ratio = Column(Float, nullable=True)
    sig_index_symbol = Column(String, nullable=True)
    sig_index_trend = Column(String, nullable=True)
    sig_index_bb_zone = Column(String, nullable=True)
    sig_index_rsi14 = Column(Float, nullable=True)
    sig_index_move_5d_pct = Column(Float, nullable=True)
    # ATR14 and it as a percent of price. True Range, not High-Low: the gap
    # is what a news catalyst produces and H-L cannot see it. See section 108.
    sig_atr14 = Column(Float, nullable=True)
    sig_atr_pct = Column(Float, nullable=True)
    # Headline COUNT for this name in the lookback window, and the most
    # recent one. A count, not a sentiment score -- attention is a fact,
    # sentiment would be an unmeasured model sitting next to measured columns.
    # NET position greeks -- the spread's, not a leg's. short_delta above
    # describes one contract; these describe the trade. See greeks.py.
    sig_net_delta = Column(Float, nullable=True)
    sig_net_gamma = Column(Float, nullable=True)
    sig_net_theta = Column(Float, nullable=True)
    sig_net_vega = Column(Float, nullable=True)
    sig_news_count_3d = Column(Integer, nullable=True)
    sig_news_latest = Column(String, nullable=True)

    # The live order behind this row, when it was actually traded. NULL on
    # every observation-only row, which is all of them before 2026-09-01.
    # live_qty is what FILLED, not what was requested: Tradier clamps and
    # can fill partially, and the close has to be sized on the fill.
    live_order_id = Column(String, nullable=True)
    live_qty = Column(Integer, nullable=True)
    live_close_order_id = Column(String, nullable=True)
    live_closed_at = Column(DateTime(timezone=True), nullable=True)
