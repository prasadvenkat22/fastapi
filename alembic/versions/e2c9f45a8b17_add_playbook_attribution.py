"""record which named strategy opened each position

Revision ID: e2c9f45a8b17
Revises: d4b8e13c7a95
Create Date: 2026-08-17 00:00:00.000000

Strike placement is now chosen by time of day (trading_engine/playbook.py),
so a single day can produce trades from several different strategies. Without
recording which one opened a position, their outcomes pool together and there
is no way to tell a strategy that earns its place from one that does not --
which is the whole point of running them separately.

Nullable: trades that closed before this existed have no window to attribute,
and guessing one from opened_at would invent history.
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'e2c9f45a8b17'
down_revision: Union[str, None] = 'd4b8e13c7a95'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    for table in ("trading_open_positions", "trading_history"):
        op.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS playbook VARCHAR")
        op.execute(f"CREATE INDEX IF NOT EXISTS ix_{table}_playbook ON {table} (playbook)")


def downgrade() -> None:
    for table in ("trading_open_positions", "trading_history"):
        op.execute(f"DROP INDEX IF EXISTS ix_{table}_playbook")
        op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS playbook")
