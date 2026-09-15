"""keep the raw crude/10Y/VIX levels, so intraday CHANGE can be measured

The objective macro read scores each channel as its change FROM THE SESSION
OPEN. That is the right measure of where the day sits and the wrong one for
where it is going: yields opening at 5.00%, spiking to 5.10% by 11:00 and
easing to 5.05% by 14:00 still read +5bp from the open, and so still read
risk-off, after three hours of falling. A reversal that does not cross back
through the open is invisible to a from-open measure.

Storing the raw level on every row makes the incremental change computable from
this table's own history -- no extra feed, no second API, just a lookback to
the row from an hour ago.

Revision ID: c5f2a8e14b73
Revises: b4e7d13f92a6
"""
from typing import Sequence, Union

from alembic import op

revision: str = "c5f2a8e14b73"
down_revision: Union[str, None] = "b4e7d13f92a6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE symbol_sentiment_hourly "
               "ADD COLUMN IF NOT EXISTS raw JSONB")


def downgrade() -> None:
    op.execute("ALTER TABLE symbol_sentiment_hourly DROP COLUMN IF EXISTS raw")
