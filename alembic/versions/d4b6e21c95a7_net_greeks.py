"""net position greeks on weekly_shadow

short_delta describes one contract; it does not describe the spread. Measured
on the live SNDK 1600/1700 chain 2026-09-06: both legs bleed theta and both are
long vega, while the SPREAD earns +1.92/day of theta and is SHORT vega at
-0.271. For a deep-ITM structure those are the entire P&L engine -- max profit
(+32.90) and negative extrinsic (-32.90) are the same number, and theta and
vega are what collect it. None of that was visible in any recorded row.

Revision ID: d4b6e21c95a7
Revises: c7d1e93a48f0
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d4b6e21c95a7"
down_revision: Union[str, None] = "c7d1e93a48f0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_COLUMNS = (
    ("sig_net_delta", "DOUBLE PRECISION"),
    ("sig_net_gamma", "DOUBLE PRECISION"),
    ("sig_net_theta", "DOUBLE PRECISION"),
    ("sig_net_vega", "DOUBLE PRECISION"),
)


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
