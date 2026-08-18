"""share the macro read across processes

Revision ID: f3d7b91c5e28
Revises: e2c9f45a8b17
Create Date: 2026-08-18 00:00:00.000000

The macro verdict was cached in a module global, which only worked while the
in-app scheduler held one long-lived process. Cron runs a fresh process every
minute, so the cache never hit and every cycle paid full price: three RSS
scrapes, two Voyage embeddings and a Claude call. Voyage's free tier is 3
requests per minute, so this failed within seconds of cron working.
"""
from typing import Sequence, Union

from alembic import op

revision: str = 'f3d7b91c5e28'
down_revision: Union[str, None] = 'e2c9f45a8b17'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS trading_macro_cache (
            id INTEGER PRIMARY KEY,
            verdict VARCHAR NOT NULL,
            confidence FLOAT,
            risk_factor VARCHAR,
            updated_at TIMESTAMPTZ DEFAULT now()
        )
    """)
    op.execute("ALTER TABLE trading_macro_cache ALTER COLUMN updated_at SET DEFAULT now()")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS trading_macro_cache")
