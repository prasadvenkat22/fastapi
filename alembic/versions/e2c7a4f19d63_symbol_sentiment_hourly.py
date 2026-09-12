"""hourly per-symbol sentiment from every source, scored against nothing yet

THREE CANDIDATES, STORED SIDE BY SIDE, GATING NOTHING.

    polygon   ticker-level sentiment with reasoning, free with the news call
    finbert   local classifier over the same headlines
    haiku     the 09:30 reasoned verdict, already in news_verdicts

Measured 2026-09-12 against the graded outcomes, lookahead removed and
session-clustered:

    Haiku, non-neutral      44 symbol-days   59.1%
    FinBERT, any signal    147 symbol-days   52.4%
    FinBERT, |score|>0.10  108 symbol-days   50.0%

FinBERT's "negative" days averaged +0.061% -- it does not find the direction
it names. Polygon's own sentiment has never been scored at all. So none of
them gates a trade from this table; they accumulate beside each other and
verdict_outcome decides in twenty sessions, which is the same discipline
weekly_shadow and dte0_shadow follow.

WHY POLYGON REPLACES THE SCRAPE. Articles arrive TICKER-TAGGED, which deletes
the entire alias-matching layer and the three bug classes it produced in one
evening: ticker-only patterns that could not see a sector story, sector terms
that matched nothing, and a MarketWatch feed that answered 200 for months
while serving headlines a year old. A dead RSS feed and a quiet news day are
indistinguishable; a Polygon 429 is not.

Rate limit measured, not assumed: the free tier refused the 6th call in two
seconds and returned 429 eight times out of eight when hammered. It needs
~13-15s BETWEEN calls, not a sleep every fourth one.

Revision ID: e2c7a4f19d63
Revises: d4b91e7a2c68
"""
from typing import Sequence, Union

from alembic import op

revision: str = "e2c7a4f19d63"
down_revision: Union[str, None] = "d4b91e7a2c68"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS symbol_sentiment_hourly (
            id SERIAL PRIMARY KEY,
            symbol VARCHAR NOT NULL,
            asof TIMESTAMPTZ NOT NULL,
            trading_day DATE NOT NULL,
            -- 'polygon' | 'finbert'
            source VARCHAR NOT NULL,
            -- positive | negative | neutral, the vocabulary both sources share
            label VARCHAR,
            -- signed: positive minus negative, so the two sources compare
            score DOUBLE PRECISION,
            headline_count INTEGER,
            -- Polygon ships reasoning with its sentiment; FinBERT cannot.
            -- Stored because a verdict you cannot interrogate is a verdict you
            -- cannot debug, which is what made Friday's SNDK read diagnosable.
            rationale TEXT,
            recorded_at TIMESTAMPTZ DEFAULT now()
        )
    """)
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_symbol_sentiment_hour "
               "ON symbol_sentiment_hourly (symbol, source, asof)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_symbol_sentiment_day "
               "ON symbol_sentiment_hourly (trading_day DESC, symbol)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS symbol_sentiment_hourly")
