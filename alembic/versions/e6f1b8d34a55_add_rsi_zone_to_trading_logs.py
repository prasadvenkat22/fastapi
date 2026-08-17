"""add rsi_zone column to trading_logs

Revision ID: e6f1b8d34a55
Revises: d5e9a3f7c221
Create Date: 2026-08-17 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'e6f1b8d34a55'
down_revision: Union[str, None] = 'd5e9a3f7c221'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Guarded — safe on a fresh database (where main.py's
    # Base.metadata.create_all() may create trading_logs first, since
    # alembic runs before that startup hook) and on an existing one alike.
    op.execute("ALTER TABLE IF EXISTS trading_logs ADD COLUMN IF NOT EXISTS rsi_zone VARCHAR")


def downgrade() -> None:
    op.execute("ALTER TABLE IF EXISTS trading_logs DROP COLUMN IF EXISTS rsi_zone")
