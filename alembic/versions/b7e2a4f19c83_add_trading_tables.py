"""add trading_logs and market_news_vectors tables

Revision ID: b7e2a4f19c83
Revises: a3f1c9d02b47
Create Date: 2026-08-16 19:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'b7e2a4f19c83'
down_revision: Union[str, None] = 'a3f1c9d02b47'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Guarded with IF NOT EXISTS throughout — safe on a fresh database (where
    # main.py's Base.metadata.create_all() may create these tables first, since
    # alembic runs before that startup hook) and on an existing one alike.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.execute("""
        CREATE TABLE IF NOT EXISTS trading_logs (
            id UUID PRIMARY KEY,
            timestamp TIMESTAMPTZ DEFAULT now(),
            execution_status VARCHAR NOT NULL,
            macd_signal VARCHAR,
            sma_trend VARCHAR,
            bollinger_zone VARCHAR,
            market_sentiment VARCHAR,
            raw_log_payload JSONB
        )
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS market_news_vectors (
            id UUID PRIMARY KEY,
            headline_text VARCHAR NOT NULL,
            publication_date TIMESTAMPTZ DEFAULT now(),
            text_embedding VECTOR(1024)
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS market_news_vectors")
    op.execute("DROP TABLE IF EXISTS trading_logs")
