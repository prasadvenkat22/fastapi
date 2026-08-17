"""add trade_setup_vectors table and entry-signal columns

Revision ID: f2a7c5e91b03
Revises: e6f1b8d34a55
Create Date: 2026-08-17 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'f2a7c5e91b03'
down_revision: Union[str, None] = 'e6f1b8d34a55'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Guarded throughout — safe on a fresh database (where main.py's
    # Base.metadata.create_all() may create these tables first, since
    # alembic runs before that startup hook) and on an existing one alike.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.execute("ALTER TABLE IF EXISTS trading_open_positions ADD COLUMN IF NOT EXISTS entry_macd_signal VARCHAR")
    op.execute("ALTER TABLE IF EXISTS trading_open_positions ADD COLUMN IF NOT EXISTS entry_sma_trend VARCHAR")
    op.execute("ALTER TABLE IF EXISTS trading_open_positions ADD COLUMN IF NOT EXISTS entry_bollinger_zone VARCHAR")
    op.execute("ALTER TABLE IF EXISTS trading_open_positions ADD COLUMN IF NOT EXISTS entry_rsi_zone VARCHAR")

    op.execute("ALTER TABLE IF EXISTS trading_history ADD COLUMN IF NOT EXISTS entry_macd_signal VARCHAR")
    op.execute("ALTER TABLE IF EXISTS trading_history ADD COLUMN IF NOT EXISTS entry_sma_trend VARCHAR")
    op.execute("ALTER TABLE IF EXISTS trading_history ADD COLUMN IF NOT EXISTS entry_bollinger_zone VARCHAR")
    op.execute("ALTER TABLE IF EXISTS trading_history ADD COLUMN IF NOT EXISTS entry_rsi_zone VARCHAR")

    op.execute("""
        CREATE TABLE IF NOT EXISTS trade_setup_vectors (
            id UUID PRIMARY KEY,
            setup_text VARCHAR NOT NULL,
            strategy VARCHAR NOT NULL,
            macd_signal VARCHAR,
            sma_trend VARCHAR,
            bollinger_zone VARCHAR,
            rsi_zone VARCHAR,
            realized_pnl_pct FLOAT NOT NULL,
            close_reason VARCHAR NOT NULL,
            closed_at TIMESTAMPTZ DEFAULT now(),
            setup_embedding VECTOR(1024)
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS trade_setup_vectors")
    op.execute("ALTER TABLE IF EXISTS trading_history DROP COLUMN IF EXISTS entry_rsi_zone")
    op.execute("ALTER TABLE IF EXISTS trading_history DROP COLUMN IF EXISTS entry_bollinger_zone")
    op.execute("ALTER TABLE IF EXISTS trading_history DROP COLUMN IF EXISTS entry_sma_trend")
    op.execute("ALTER TABLE IF EXISTS trading_history DROP COLUMN IF EXISTS entry_macd_signal")
    op.execute("ALTER TABLE IF EXISTS trading_open_positions DROP COLUMN IF EXISTS entry_rsi_zone")
    op.execute("ALTER TABLE IF EXISTS trading_open_positions DROP COLUMN IF EXISTS entry_bollinger_zone")
    op.execute("ALTER TABLE IF EXISTS trading_open_positions DROP COLUMN IF EXISTS entry_sma_trend")
    op.execute("ALTER TABLE IF EXISTS trading_open_positions DROP COLUMN IF EXISTS entry_macd_signal")
