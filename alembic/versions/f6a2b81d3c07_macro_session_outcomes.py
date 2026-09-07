"""one row per session: the morning's macro read against what the day did

The LLM macro gate was switched off on 2026-08-25 after refusing 55 of 55
cycles on a day QQQ rose $6.50 off its low. That was a real observation and a
thin basis for a permanent decision -- two days, reconstructed by elimination
because the verdict was overwritten in a single row rather than kept.

This table is what makes the question answerable instead of arguable. One row
per trading day carrying the morning's reads and the session's outcome, so
after twenty sessions two things can be measured that August could not:

    did BEARISH mornings precede losing sessions
    how many WINNING sessions would a gate have refused

The second is the one that killed the gate, and it is the one a size-scaler
answers differently from a refusal.

Revision ID: f6a2b81d3c07
Revises: b5c8d270ae43
"""
from typing import Sequence, Union

from alembic import op

revision: str = "f6a2b81d3c07"
down_revision: Union[str, None] = "b5c8d270ae43"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS macro_session_outcomes (
            trading_day DATE PRIMARY KEY,
            -- the 09:30 reads
            qqq_news_verdict VARCHAR,
            qqq_news_confidence DOUBLE PRECISION,
            qqq_news_headlines INTEGER,
            macro_gate_verdict VARCHAR,
            macro_gate_confidence DOUBLE PRECISION,
            -- what the session did
            qqq_open DOUBLE PRECISION,
            qqq_close DOUBLE PRECISION,
            qqq_ret_pct DOUBLE PRECISION,
            qqq_move_atr DOUBLE PRECISION,
            qqq_atr14 DOUBLE PRECISION,
            -- what the engine did
            engine_trades INTEGER,
            engine_pnl DOUBLE PRECISION,
            recorded_at TIMESTAMPTZ DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_macro_outcomes_day "
               "ON macro_session_outcomes (trading_day DESC)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS macro_session_outcomes")
