"""add trading_open_positions table

Revision ID: c4d8f6a2e910
Revises: b7e2a4f19c83
Create Date: 2026-08-17 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c4d8f6a2e910'
down_revision: Union[str, None] = 'b7e2a4f19c83'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Guarded with IF NOT EXISTS — safe on a fresh database (where
    # main.py's Base.metadata.create_all() may create this table first, since
    # alembic runs before that startup hook) and on an existing one alike.
    op.execute("""
        CREATE TABLE IF NOT EXISTS trading_open_positions (
            id UUID PRIMARY KEY,
            strategy VARCHAR NOT NULL,
            underlying VARCHAR NOT NULL,
            quantity INTEGER NOT NULL,
            long_strike FLOAT NOT NULL,
            short_strike FLOAT NOT NULL,
            entry_net_debit FLOAT NOT NULL,
            opened_at TIMESTAMPTZ DEFAULT now()
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS trading_open_positions")
