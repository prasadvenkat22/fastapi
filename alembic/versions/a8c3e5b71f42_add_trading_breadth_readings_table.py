"""add trading_breadth_readings table

Revision ID: a8c3e5b71f42
Revises: f2a7c5e91b03
Create Date: 2026-08-17 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a8c3e5b71f42'
down_revision: Union[str, None] = 'f2a7c5e91b03'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Guarded with IF NOT EXISTS — safe on a fresh database (where
    # main.py's Base.metadata.create_all() may create this table first, since
    # alembic runs before that startup hook) and on an existing one alike.
    op.execute("""
        CREATE TABLE IF NOT EXISTS trading_breadth_readings (
            id UUID PRIMARY KEY,
            addq FLOAT NOT NULL,
            advancers INTEGER NOT NULL,
            decliners INTEGER NOT NULL,
            unchanged INTEGER NOT NULL,
            basket_size INTEGER NOT NULL,
            net_ratio FLOAT NOT NULL,
            recorded_at TIMESTAMPTZ DEFAULT now()
        )
    """)
    # Every read is "readings from the current session, newest first".
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_trading_breadth_readings_recorded_at
        ON trading_breadth_readings (recorded_at)
    """)
    # Applied unconditionally rather than relying on the CREATE above: when
    # create_all wins the race the CREATE is a no-op, and the table it left
    # behind has no database-level default on recorded_at. Idempotent, so
    # it is equally harmless when this migration did create the table.
    op.execute("""
        ALTER TABLE trading_breadth_readings
        ALTER COLUMN recorded_at SET DEFAULT now()
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_trading_breadth_readings_recorded_at")
    op.execute("DROP TABLE IF EXISTS trading_breadth_readings")
