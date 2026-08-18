"""store the VIX and yield readings the macro gates fire on

Revision ID: a5e8c204d19f
Revises: f3d7b91c5e28
Create Date: 2026-08-18 00:00:00.000000

Breadth was persisted; VIX and the 10-year were not, so three of the five
macro gates fired on values that left no trace. Checking whether yields had
spiked on a given day meant re-fetching from yfinance -- which is not a
record, and would have been impossible once the vendor aged the data out.
"""
from typing import Sequence, Union

from alembic import op

revision: str = 'a5e8c204d19f'
down_revision: Union[str, None] = 'f3d7b91c5e28'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS trading_macro_readings (
            id UUID PRIMARY KEY,
            vix_level FLOAT NOT NULL,
            vix_session_open FLOAT NOT NULL,
            vix_change_pct FLOAT NOT NULL,
            tnx_level FLOAT NOT NULL,
            tnx_session_open FLOAT NOT NULL,
            tnx_change_bps FLOAT NOT NULL,
            recorded_at TIMESTAMPTZ DEFAULT now()
        )
    """)
    op.execute("ALTER TABLE trading_macro_readings ALTER COLUMN recorded_at SET DEFAULT now()")
    op.execute("CREATE INDEX IF NOT EXISTS ix_trading_macro_readings_recorded_at ON trading_macro_readings (recorded_at)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_trading_macro_readings_recorded_at")
    op.execute("DROP TABLE IF EXISTS trading_macro_readings")
