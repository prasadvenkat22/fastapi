"""append-only history of the macro verdict

trading_macro_cache is ONE ROW, upserted. Every verdict the macro model has
ever produced overwrote the previous one, so after weeks of running at a
five-minute cadence there was nothing to analyse: no way to ask how often the
read flips, whether BAD precedes a down session, or whether re-enabling
TRADING_MACRO_LLM_GATE could be justified on evidence.

That is what made the old cadence pure cost -- the call was paid for and the
answer discarded. VIX and the 10-year were already persisted per cycle in
trading_macro_readings; the verdict derived from them was not.

Revision ID: b5c8d270ae43
Revises: e8f3a07b16c2
"""
from typing import Sequence, Union

from alembic import op

revision: str = "b5c8d270ae43"
down_revision: Union[str, None] = "e8f3a07b16c2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS trading_macro_verdicts (
            id BIGSERIAL PRIMARY KEY,
            verdict VARCHAR NOT NULL,
            confidence DOUBLE PRECISION,
            risk_factor VARCHAR,
            recorded_at TIMESTAMPTZ DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_macro_verdicts_at "
               "ON trading_macro_verdicts (recorded_at DESC)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS trading_macro_verdicts")
