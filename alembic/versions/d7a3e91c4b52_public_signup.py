"""public sign-up: users.pending_verification and email_verification_tokens

Revision ID: d7a3e91c4b52
Revises: b3f8d21c6e94
Create Date: 2026-09-23

Until now every account was made by an admin. Public sign-up creates role
'user' accounts that cannot log in until the emailed link is opened.

pending_verification defaults to false, so every existing (admin-made)
account keeps working with no backfill.

Guarded with IF NOT EXISTS like every table migration here: main.py's
create_all can create the table first, and an unguarded CREATE would stop
"alembic upgrade head && uvicorn" before the API starts.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "d7a3e91c4b52"
down_revision: Union[str, None] = "b3f8d21c6e94"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE users
        ADD COLUMN IF NOT EXISTS pending_verification BOOLEAN NOT NULL DEFAULT false
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS email_verification_tokens (
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
        CREATE UNIQUE INDEX IF NOT EXISTS ix_email_verification_tokens_token_hash
        ON email_verification_tokens (token_hash)
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_email_verification_tokens_user_id
        ON email_verification_tokens (user_id)
    """)
    # Sign-up assigns 'user'; c1f7a92e4d68 seeds it, but do not depend on
    # nobody having deleted it through the roles CRUD since.
    op.execute("""
        INSERT INTO roles (role, description)
        SELECT 'user', 'Read access to business records'
        WHERE NOT EXISTS (SELECT 1 FROM roles WHERE role = 'user')
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS email_verification_tokens")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS pending_verification")
