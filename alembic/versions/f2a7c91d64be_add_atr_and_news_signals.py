"""add ATR and per-symbol news columns to weekly_shadow

The book sized every width on mean(High-Low), which cannot see a gap -- and a
gap is exactly what a news catalyst produces. Measured 2026-09-06 across the
tracked names, High-Low understates the true daily move by 4% (DELL) to 29%
(MRVL), QQQ by 23%. SNDK's 09-04 session: High-Low 159.00, True Range 185.01.

The news columns close a gap that had been open since the engine was built:
market_news_vectors held 2,321 embedded headlines and none was ever attached
to a position, because the engine holds tickers and the wires write company
names ("SNDK" 0 headlines, "SanDisk" 14). Two SanDisk headlines were captured
on 2026-09-04 and were invisible to a book holding SNDK.

Revision ID: f2a7c91d64be
Revises: e4b8c2a71d59
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f2a7c91d64be"
down_revision: Union[str, None] = "e4b8c2a71d59"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_COLUMNS = (
    ("sig_atr14", "DOUBLE PRECISION"),
    ("sig_atr_pct", "DOUBLE PRECISION"),
    ("sig_news_count_3d", "INTEGER"),
    ("sig_news_latest", "VARCHAR"),
)


def _weekly_shadow_missing() -> bool:
    """True on a database where weekly_shadow has not been created yet.

    Same guard as the three revisions before this one: the table has no CREATE
    migration and is built by main.py's startup create_all(), which runs AFTER
    `alembic upgrade head` in the compose command. Without this a fresh
    database crash-loops on UndefinedTable before uvicorn ever starts.
    """
    return not sa.inspect(op.get_bind()).has_table("weekly_shadow")


def upgrade() -> None:
    if _weekly_shadow_missing():
        return
    for name, sqltype in _COLUMNS:
        op.execute(f"ALTER TABLE weekly_shadow ADD COLUMN IF NOT EXISTS {name} {sqltype}")


def downgrade() -> None:
    if _weekly_shadow_missing():
        return
    for name, _ in _COLUMNS:
        op.execute(f"ALTER TABLE weekly_shadow DROP COLUMN IF EXISTS {name}")
