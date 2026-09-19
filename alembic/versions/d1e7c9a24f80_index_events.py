"""index_events: an index inclusion or removal as a dated row

Section 197. Detected from stored headlines by trading_engine.index_events;
read by scripts/index_event_trade.py. One row per (symbol, index, action,
effective date), so the same release carried by six wires is one event.

Revision ID: d1e7c9a24f80
Revises: c5f2a8e14b73
"""
from typing import Sequence, Union

from alembic import op

revision: str = "d1e7c9a24f80"
down_revision: Union[str, None] = "c5f2a8e14b73"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS index_events (
            id SERIAL PRIMARY KEY,
            symbol VARCHAR(12) NOT NULL,
            index_name VARCHAR(40) NOT NULL,
            action VARCHAR(8) NOT NULL,
            announced_at TIMESTAMPTZ,
            effective_date DATE NOT NULL,
            basis VARCHAR(80),
            headline VARCHAR(300),
            guid VARCHAR(500),
            source VARCHAR(60),
            traded_order_id VARCHAR(40),
            traded_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ DEFAULT now(),
            UNIQUE (symbol, index_name, action, effective_date)
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_index_events_eff ON index_events (effective_date)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS index_events")
