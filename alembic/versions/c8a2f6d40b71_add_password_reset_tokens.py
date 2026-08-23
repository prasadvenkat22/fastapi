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

GUARDED WITH IF NOT EXISTS, like every other table migration here. The first
version of this file was not, and it took the API down: the container starts
with "alembic upgrade head && uvicorn", something had already created the
table through a Base.metadata.create_all(), alembic died on DuplicateTable,
and the && meant uvicorn never started. The import-time create_all that
caused it is gone now, but main.py still has one in its startup hook, and
every neighbouring migration guards against precisely this. Being the one
that does not is how it happens again.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "c8a2f6d40b71"
down_revision: Union[str, None] = "b7f4d3c81a26"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS password_reset_tokens (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
            token_hash VARCHAR NOT NULL,
            expires_at TIMESTAMPTZ NOT NULL,
            used_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ DEFAULT now(),
            requested_ip VARCHAR
        )
    """)
    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS ix_password_reset_tokens_token_hash
        ON password_reset_tokens (token_hash)
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_password_reset_tokens_user_id
        ON password_reset_tokens (user_id)
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS password_reset_tokens")
