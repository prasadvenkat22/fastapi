"""subscribers: the product-updates list

Not accounts. A visitor who wants news about the auto-trader or the practice
leaves an address; they confirm it by clicking a link (double opt-in), and
every message they ever get carries an unsubscribe link built from the same
token. No password, no role, no access to anything -- desk access stays with
accounts an admin creates. Section 203.

Revision ID: b3f8d21c6e94
Revises: a9c4e17b52d3
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b3f8d21c6e94"
down_revision: Union[str, None] = "a9c4e17b52d3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("subscribers"):
        return  # create_all on a fresh database already built it
    op.create_table(
        "subscribers",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("email", sa.String, nullable=False, unique=True, index=True),
        sa.Column("name", sa.String, nullable=True),
        sa.Column("interest", sa.String, nullable=True),
        sa.Column("source", sa.String, nullable=True),
        sa.Column("token_hash", sa.String, nullable=False, unique=True, index=True),
        sa.Column("token_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("unsubscribed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("requested_ip", sa.String, nullable=True),
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS subscribers")
