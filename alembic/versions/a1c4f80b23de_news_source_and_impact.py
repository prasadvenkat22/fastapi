"""news source column + per-symbol forward-return labels

WHAT THIS MAKES POSSIBLE. Until now market_news_vectors could answer "what was
said" and nothing else: no source, no symbol, no outcome. So no sentiment
scheme -- LLM, keyword, or a hand-tuned source-weight matrix -- could be
tested, only asserted. Two additions fix that:

  market_news_vectors.source   which feed the headline came from. Without it
                               "weight Bloomberg above a retail blog" has
                               nothing to key on; _scrape_headlines() was
                               discarding the origin.

  news_symbol_impact           one row per (headline, symbol) with the price
                               that followed. Forward returns are stored in
                               ATR units as well as percent, because a 3% move
                               in NVDA and a 3% move in SNDK are not the same
                               event and a mixed-name study that treats them
                               alike measures volatility, not news.

Revision ID: a1c4f80b23de
Revises: f2a7c91d64be
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a1c4f80b23de"
down_revision: Union[str, None] = "f2a7c91d64be"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if insp.has_table("market_news_vectors"):
        op.execute("ALTER TABLE market_news_vectors ADD COLUMN IF NOT EXISTS source VARCHAR")
    # Created here rather than left to create_all, so a fresh database gets it
    # from the migration and an existing one gets it now.
    op.execute("""
        CREATE TABLE IF NOT EXISTS news_symbol_impact (
            id UUID PRIMARY KEY,
            news_id UUID,
            symbol VARCHAR NOT NULL,
            headline_text VARCHAR NOT NULL,
            source VARCHAR,
            published_on DATE NOT NULL,
            spot_at_publish DOUBLE PRECISION,
            atr14_at_publish DOUBLE PRECISION,
            ret_1d_pct DOUBLE PRECISION,
            ret_5d_pct DOUBLE PRECISION,
            move_1d_atr DOUBLE PRECISION,
            move_5d_atr DOUBLE PRECISION,
            sentiment VARCHAR,
            sentiment_confidence DOUBLE PRECISION,
            sentiment_rationale VARCHAR,
            labelled_at TIMESTAMPTZ DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_news_impact_symbol_day "
               "ON news_symbol_impact (symbol, published_on)")
    # One row per headline per symbol. Without this a re-run of the backfill
    # doubles every row and every future study silently double-counts.
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_news_impact_news_symbol "
               "ON news_symbol_impact (news_id, symbol)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS news_symbol_impact")
    op.execute("ALTER TABLE market_news_vectors DROP COLUMN IF EXISTS source")
