"""record WHEN an open position set its best return

Revision ID: d1f7a03c9e84
Revises: c8e2a94f6b71
Create Date: 2026-08-29 00:00:00.000000

peak_return_pct records HOW GOOD a position has been and nothing about when.
Every profit-protection rule built so far could therefore only ask "how far
below the peak are we" -- a giveback -- and a giveback cannot tell a dip
inside a climb from the end of one.

The stalled-peak rule asks a different question: has the position made a NEW
high recently? While it keeps setting higher highs the rule waits, however
deep the pullback; once it has gone TRADING_STALL_MINUTES without a new high
AND sits TRADING_STALL_GIVEBACK_PCT below the peak, it books. Answering that
needs the peak's TIMESTAMP, which is this column.

Measured before being wired, and it is NOT a free win: over 60 sessions the
best arm returns +13.10 a day against +38.39 for riding to the handoff, so it
costs about $25 a day and buys a worst day $234 shallower plus two points of
green-day frequency. Deployed as a stated risk preference against that
measurement -- see section 43.

Nullable: positions opened before this have no peak time, and the exit rule
treats a null as "no peak recorded yet" rather than inventing one.
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'd1f7a03c9e84'
down_revision: Union[str, None] = 'c8e2a94f6b71'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE trading_open_positions "
        "ADD COLUMN IF NOT EXISTS peak_at TIMESTAMP WITH TIME ZONE"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE trading_open_positions DROP COLUMN IF EXISTS peak_at")
