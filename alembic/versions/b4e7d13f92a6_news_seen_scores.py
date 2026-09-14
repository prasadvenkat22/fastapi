"""keep each scored headline's direction, so the verdict can use a rolling window

GUID dedupe stops an article being SCORED twice, which is right -- it is the
same article. But the verdict was also being built only from articles new since
the last sweep, and those are two different questions.

At 09:25 a sweep sees a full 24h of macro news and produces a verdict from a
dozen topics. At 11:25 it sees whatever published in the last hour -- two or
three articles, usually below MIN_TOPICS -- so no verdict is written and the
gates go on reading the 09:25 row. The hourly re-grade collapses back to
once-a-day, silently, and looks like a quiet tape rather than a broken window.

So the score is kept ON the dedupe row. Scoring stays once per article; the
verdict aggregates every article scored inside the lookback window, new or not.

Revision ID: b4e7d13f92a6
Revises: a1c9e42f80d7
"""
from typing import Sequence, Union

from alembic import op

revision: str = "b4e7d13f92a6"
down_revision: Union[str, None] = "a1c9e42f80d7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE news_seen ADD COLUMN IF NOT EXISTS rule_dir SMALLINT")
    op.execute("ALTER TABLE news_seen ADD COLUMN IF NOT EXISTS topic VARCHAR")
    op.execute("ALTER TABLE news_seen ADD COLUMN IF NOT EXISTS published TIMESTAMPTZ")
    op.execute("ALTER TABLE news_seen ADD COLUMN IF NOT EXISTS scored_at TIMESTAMPTZ")
    # The verdict query is "every scored row inside the window", so it reads on
    # published time and skips the unscored.
    op.execute("CREATE INDEX IF NOT EXISTS ix_news_seen_scored "
               "ON news_seen (published DESC) WHERE rule_dir IS NOT NULL")


def downgrade() -> None:
    for c in ("rule_dir", "topic", "published", "scored_at"):
        op.execute(f"ALTER TABLE news_seen DROP COLUMN IF EXISTS {c}")
