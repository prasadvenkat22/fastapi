"""weekly-anchored VWAP on weekly_shadow

weekly_signals.py excluded VWAP with the argument that "a weekly position spans
five sessions and five VWAPs, so there is no single value to record". That rules
out the SESSION form only. A VWAP ANCHORED to Monday's open is one value across
the whole week, does not reset under the trade, and is the level the week's flow
actually transacted at -- which is the reference a five-day hold wants on day
four, and precisely what the original argument assumed could not exist.

Approximated from daily bars (typical price weighted by daily volume), so the
column reads as "roughly where the week's volume transacted" rather than an
exact intraday figure.

Revision ID: e8f3a07b16c2
Revises: d4b6e21c95a7
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e8f3a07b16c2"
down_revision: Union[str, None] = "d4b6e21c95a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_COLUMNS = (("sig_vwap_week", "DOUBLE PRECISION"), ("sig_vwap_side", "VARCHAR"))


def _weekly_shadow_missing() -> bool:
    """Same fresh-database guard as every weekly_shadow revision before it."""
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
