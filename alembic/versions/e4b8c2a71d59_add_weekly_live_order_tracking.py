"""track the live order behind a weekly shadow row

Revision ID: e4b8c2a71d59
Revises: d1f7a03c9e84
Create Date: 2026-09-01 00:00:00.000000

weekly_shadow has been observation-only since it was written: it records a
structure every Friday and marks it until expiry without placing anything.
Trading one of those rows for real needs the row to know which order it
belongs to, how many contracts actually filled, and whether it has been
closed -- otherwise a restart cannot tell a live position from a paper one.

Deliberately four narrow columns rather than a second table. A traded row IS
a shadow row that happened to be ordered; splitting them would mean the
outcome columns (target_hit_at, expiry_value) live in one place and the fill
in another, and the whole point of this book is comparing what was booked
against what holding would have paid.

live_qty is the contracts FILLED, not requested. Tradier clamps and can fill
partially, and the difference matters when the close is sized.

All nullable: every row written before this is a pure observation and must
stay one.
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'e4b8c2a71d59'
down_revision: Union[str, None] = 'd1f7a03c9e84'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_COLUMNS = (
    ("live_order_id", "VARCHAR"),
    ("live_qty", "INTEGER"),
    ("live_close_order_id", "VARCHAR"),
    ("live_closed_at", "TIMESTAMP WITH TIME ZONE"),
)


def upgrade() -> None:
    for name, sqltype in _COLUMNS:
        op.execute(f"ALTER TABLE weekly_shadow ADD COLUMN IF NOT EXISTS {name} {sqltype}")


def downgrade() -> None:
    for name, _ in _COLUMNS:
        op.execute(f"ALTER TABLE weekly_shadow DROP COLUMN IF EXISTS {name}")
