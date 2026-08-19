"""track each position's best mark so profit can be ratcheted

Revision ID: b7f4d3c81a26
Revises: a5e8c204d19f
Create Date: 2026-08-19 00:00:00.000000

The trailing exit followed the 9 EMA, which is a PRICE trail and knows
nothing about the position's P&L. A spread can hand back most of its gain
before price crosses the 9 EMA, because spread value moves nonlinearly with
price and time decay drains it independently. Recording the peak lets the
engine exit on a retracement from the best mark instead.
"""
from typing import Sequence, Union

from alembic import op

revision: str = 'b7f4d3c81a26'
down_revision: Union[str, None] = 'a5e8c204d19f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE trading_open_positions ADD COLUMN IF NOT EXISTS peak_return_pct FLOAT DEFAULT 0")


def downgrade() -> None:
    op.execute("ALTER TABLE trading_open_positions DROP COLUMN IF EXISTS peak_return_pct")
