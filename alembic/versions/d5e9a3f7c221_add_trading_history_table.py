"""add trading_history table

Revision ID: d5e9a3f7c221
Revises: c4d8f6a2e910
Create Date: 2026-08-17 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'd5e9a3f7c221'
down_revision: Union[str, None] = 'c4d8f6a2e910'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Guarded with IF NOT EXISTS — safe on a fresh database (where
    # main.py's Base.metadata.create_all() may create this table first, since
    # alembic runs before that startup hook) and on an existing one alike.
    op.execute("""
        CREATE TABLE IF NOT EXISTS trading_history (
            id UUID PRIMARY KEY,
            strategy VARCHAR NOT NULL,
            underlying VARCHAR NOT NULL,
            quantity INTEGER NOT NULL,
            long_strike FLOAT NOT NULL,
            short_strike FLOAT NOT NULL,
            entry_net_debit FLOAT NOT NULL,
            exit_net_value FLOAT NOT NULL,
            realized_pnl_dollars FLOAT NOT NULL,
            realized_pnl_pct FLOAT NOT NULL,
            close_reason VARCHAR NOT NULL,
            opened_at TIMESTAMPTZ,
            closed_at TIMESTAMPTZ DEFAULT now()
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS trading_history")
