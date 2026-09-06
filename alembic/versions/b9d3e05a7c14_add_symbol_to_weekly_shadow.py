"""add symbol to weekly shadow

Revision ID: b9d3e05a7c14
Revises: a4e8c17b92f5
Create Date: 2026-08-27

The weekly shadow has only ever watched QQQ, and the table had no way to say
so -- the symbol was implied by the fact that nothing else was written.

That stops working the moment a second underlying is marked, and a second
underlying is the whole point: the question is not "does a weekly condor
work" but "on which names, if any, does implied exceed realised", and that
cannot be answered from one symbol.

BACKFILLED TO 'QQQ' RATHER THAN LEFT NULL, then made NOT NULL. Every existing
row genuinely is QQQ, so NULL would mean "unknown" about rows that are not
unknown, and the first GROUP BY symbol would quietly split the history into a
named bucket and an anonymous one. The column is what the record already
meant, written down.

An index on (symbol, expiration) because every read path is per symbol per
expiry: the mark loop groups open rows by exactly that pair to fetch one
chain per group rather than one per row, and any later analysis compares
across symbols within an expiry.

GUARDED WITH IF NOT EXISTS, like every other migration here. The container
starts with "alembic upgrade head && uvicorn", so a migration that raises on
an already-applied change does not fail loudly in isolation -- it takes the
API down with it.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b9d3e05a7c14"
down_revision: Union[str, None] = "a4e8c17b92f5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _weekly_shadow_missing() -> bool:
    """True on a database where weekly_shadow has not been created yet.

    The table has no CREATE migration -- it is built by main.py's startup
    create_all() from models_pgdb.trading_models. On an EXISTING database that
    happened long ago and these ALTERs apply normally. On a FRESH one, compose
    runs `alembic upgrade head && uvicorn`, so alembic reaches this revision
    before the app has ever started and the ALTER kills the boot: the && never
    fires, uvicorn never runs, and the container crash-loops with
    UndefinedTable. Skipping is correct rather than merely safe -- create_all
    builds the table from the current model, which already has every column
    these revisions add.
    """
    return not sa.inspect(op.get_bind()).has_table("weekly_shadow")


def upgrade() -> None:
    if _weekly_shadow_missing():
        return
    op.execute("ALTER TABLE weekly_shadow ADD COLUMN IF NOT EXISTS symbol VARCHAR")
    # Existing rows are all QQQ. Backfill before the NOT NULL so the constraint
    # cannot fail on the history it is being added to.
    op.execute("UPDATE weekly_shadow SET symbol = 'QQQ' WHERE symbol IS NULL")
    op.execute("ALTER TABLE weekly_shadow ALTER COLUMN symbol SET NOT NULL")
    op.execute("ALTER TABLE weekly_shadow ALTER COLUMN symbol SET DEFAULT 'QQQ'")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_weekly_shadow_symbol_expiration "
        "ON weekly_shadow (symbol, expiration)"
    )


def downgrade() -> None:
    if _weekly_shadow_missing():
        return
    op.execute("DROP INDEX IF EXISTS ix_weekly_shadow_symbol_expiration")
    op.execute("ALTER TABLE weekly_shadow DROP COLUMN IF EXISTS symbol")
