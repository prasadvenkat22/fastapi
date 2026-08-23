"""add password reset tokens

Revision ID: c8a2f6d40b71
Revises: b7f4d3c81a26
Create Date: 2026-08-23

Backs the self-service forgot-password flow. Until this existed the only
recovery for a forgotten password was an administrator issuing a temporary
one by hand, which does not work at 2am and does not work at all if the
person who forgot theirs IS the only administrator.

token_hash is unique and indexed because every lookup is by hash and a
collision would hand one person another's reset.

ondelete CASCADE: deleting a user must not leave live reset tokens pointing
at a vanished row.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c8a2f6d40b71"
down_revision: Union[str, None] = "b7f4d3c81a26"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "password_reset_tokens",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("token_hash", sa.String(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=True),
        sa.Column("requested_ip", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_password_reset_tokens_user_id",
                    "password_reset_tokens", ["user_id"])
    op.create_index("ix_password_reset_tokens_token_hash",
                    "password_reset_tokens", ["token_hash"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_password_reset_tokens_token_hash",
                  table_name="password_reset_tokens")
    op.drop_index("ix_password_reset_tokens_user_id",
                  table_name="password_reset_tokens")
    op.drop_table("password_reset_tokens")
