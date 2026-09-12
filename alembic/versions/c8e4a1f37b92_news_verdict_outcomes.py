"""one row per graded verdict: what the 09:30 read said against what the day did

WHY THIS EXISTS. news_watch.py has carried the line "on the 365 labelled rows
this repository now has, being in the news predicts nothing" since it was
written, and that claim is narrower than it reads. Checked 2026-09-11:

    news_symbol_impact   365 rows, all labelled 2026-09-06
                         sentiment column EMPTY on every one of them

So what was measured is whether BEING MENTIONED precedes a move -- mean +0.086
ATR against a standard deviation of 1.178, which is noise, exactly as a
mention should be. The GRADED verdict had never been joined to an outcome at
all, and the whole set predates the window and macro-term fixes of 2026-09-07.

Joined by hand that evening across 204 verdicts and 24 sessions, the ordering
came out monotonic:

    VERY_BULLISH   5   +1.110%   60% right
    BULLISH       18   +0.995%   61%
    NEUTRAL      115   -0.011%
    BEARISH       26   -0.378%   62%
    VERY_BEARISH   1   -1.287%  100%

That is not nothing, and it is also not yet a result: all of it sits in the
PRE-FIX era, the four post-fix sessions hold four rows a bucket and point the
other way, and nothing corrects for day clustering -- fifteen correlated tech
names reading bearish on one morning is one observation, not fifteen.

A hand-join that has to be rebuilt from memory in a month is how a question
stays arguable. This table makes it accumulate: one row per verdict, graded
after the close, so the same query answers it with post-fix data in twenty
sessions.

THE HORIZON IS OPEN TO CLOSE, deliberately. The verdict is written at 09:30
from news since the previous close, so everything before the open is already
in the price; measuring from the prior close would credit the read with a gap
it could not have traded.

Revision ID: c8e4a1f37b92
Revises: a3f7c04e91b8
"""
from typing import Sequence, Union

from alembic import op

revision: str = "c8e4a1f37b92"
down_revision: Union[str, None] = "a3f7c04e91b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS news_verdict_outcomes (
            symbol VARCHAR NOT NULL,
            trading_day DATE NOT NULL,
            -- the 09:30 read, copied so a re-grade of the verdict cannot
            -- silently rewrite what an outcome was measured against
            verdict VARCHAR,
            confidence DOUBLE PRECISION,
            headline_count INTEGER,
            -- what the session did
            open_px DOUBLE PRECISION,
            close_px DOUBLE PRECISION,
            ret_pct DOUBLE PRECISION,
            atr14 DOUBLE PRECISION,
            move_atr DOUBLE PRECISION,
            -- which pipeline produced the verdict. The window and macro-term
            -- fixes landed 2026-09-07; rows either side are not comparable.
            pipeline_era VARCHAR,
            graded_at TIMESTAMPTZ DEFAULT now(),
            PRIMARY KEY (symbol, trading_day)
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_verdict_outcomes_day "
               "ON news_verdict_outcomes (trading_day DESC)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_verdict_outcomes_verdict "
               "ON news_verdict_outcomes (verdict)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS news_verdict_outcomes")
