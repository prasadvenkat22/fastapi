"""per-symbol daily news verdict, refreshed when the headline set changes

The macro headline read already runs every 5 minutes (MACRO_REFRESH_MINUTES).
The per-symbol read had no schedule at all -- classify_day() existed and
nothing called it. This table is what an hourly watcher writes to.

headline_digest is why it can run hourly without costing 105 Claude calls a
day: it is a hash of the day's headline set for the symbol, so an unchanged
set skips the model entirely. A quiet name costs one SELECT an hour; a name
that just broke news is re-graded within the hour.

Revision ID: c7d1e93a48f0
Revises: a1c4f80b23de
"""
from typing import Sequence, Union

from alembic import op

revision: str = "c7d1e93a48f0"
down_revision: Union[str, None] = "a1c4f80b23de"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS news_verdicts (
            id UUID PRIMARY KEY,
            symbol VARCHAR NOT NULL,
            trading_day DATE NOT NULL,
            verdict VARCHAR NOT NULL,
            confidence DOUBLE PRECISION,
            rationale VARCHAR,
            headline_count INTEGER,
            headline_digest VARCHAR,
            suggested_structure VARCHAR,
            position_action VARCHAR,
            created_at TIMESTAMPTZ DEFAULT now(),
            updated_at TIMESTAMPTZ DEFAULT now()
        )
    """)
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_news_verdict_symbol_day "
               "ON news_verdicts (symbol, trading_day)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS news_verdicts")
