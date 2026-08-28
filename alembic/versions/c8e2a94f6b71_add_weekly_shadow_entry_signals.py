"""record the market state each weekly shadow row was opened into

Revision ID: c8e2a94f6b71
Revises: b7f4d3c81a26
Create Date: 2026-08-28 00:00:00.000000

The weekly single-name book is to be gated on index direction, moving
averages, Bollinger position and realised-against-implied volatility. None of
that can be judged from the rows as they stand: weekly_shadow records
strikes, deltas and credits and nothing about the market around them.

Weekly option prices have no historical feed, so the gate cannot be
backtested -- the same wall the 0DTE condor and the shadow book itself hit.
The only route to an answer is to record the inputs on every row from now on
and ask the question once eight or ten Fridays exist. That is the section 22
order deliberately: shadow first, then evidence, then a decision.

sig_rv_iv_ratio is the column the whole single-name question turns on. Above
1.0 the underlying has been realising more than the market charges, which is
the wrong side of the variance risk premium; a rough run on 2026-08-27 put
ten of eleven names there. If that holds when measured period-matched, no
technical gate rescues the structure -- the same lesson section 21 reached
about the credit floor.

Both labels and numbers are stored. The label is what a gate reads; the
number is what lets a different threshold be tested later without waiting
another ten Fridays to collect the data again.

All nullable: every row written before this has no reading to record, and
inventing one from the opened_at date would be fabricating history.
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c8e2a94f6b71'
down_revision: Union[str, None] = 'b7f4d3c81a26'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# (column, type). VARCHAR for the labels a gate would read, DOUBLE PRECISION
# for the numbers a different threshold would be re-derived from.
_COLUMNS = (
    ("sig_trend", "VARCHAR"),
    ("sig_ema_cross", "VARCHAR"),
    ("sig_bb_zone", "VARCHAR"),
    ("sig_bb_sd", "DOUBLE PRECISION"),
    ("sig_rsi14", "DOUBLE PRECISION"),
    ("sig_sma20", "DOUBLE PRECISION"),
    ("sig_sma50", "DOUBLE PRECISION"),
    ("sig_ema20", "DOUBLE PRECISION"),
    ("sig_move_5d_pct", "DOUBLE PRECISION"),
    ("sig_rv20", "DOUBLE PRECISION"),
    ("sig_rv_iv_ratio", "DOUBLE PRECISION"),
    ("sig_index_symbol", "VARCHAR"),
    ("sig_index_trend", "VARCHAR"),
    ("sig_index_bb_zone", "VARCHAR"),
    ("sig_index_rsi14", "DOUBLE PRECISION"),
    ("sig_index_move_5d_pct", "DOUBLE PRECISION"),
)


def upgrade() -> None:
    for name, sqltype in _COLUMNS:
        op.execute(f"ALTER TABLE weekly_shadow ADD COLUMN IF NOT EXISTS {name} {sqltype}")


def downgrade() -> None:
    for name, _ in _COLUMNS:
        op.execute(f"ALTER TABLE weekly_shadow DROP COLUMN IF EXISTS {name}")
