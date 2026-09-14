"""GUID dedupe for the macro RSS sweep

Dedupe is ON GUID, not on headline text. The wires re-publish the same story
under a lightly edited headline all day; a title check treats each edit as a
new event, and the macro mean is then dominated by whichever story got
rewritten most. The feed's own id survives the rewrite.

Separate from market_news_vectors' unique index on headline_text, which solves
a different problem (near-duplicate suppression for the novelty filter) and
cannot see that two different headlines are the same article.

Revision ID: a1c9e42f80d7
Revises: f7b3d02a5e41
"""
from typing import Sequence, Union

from alembic import op

revision: str = "a1c9e42f80d7"
down_revision: Union[str, None] = "f7b3d02a5e41"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS news_seen (
            guid VARCHAR PRIMARY KEY,
            source VARCHAR,
            title VARCHAR,
            first_seen TIMESTAMPTZ DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_news_seen_first "
               "ON news_seen (first_seen DESC)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS news_seen")
