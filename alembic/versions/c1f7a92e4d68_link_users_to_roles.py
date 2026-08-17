"""link users to roles and seed the base role set

Revision ID: c1f7a92e4d68
Revises: a8c3e5b71f42
Create Date: 2026-08-17 00:00:00.000000

The roles table has existed (with full CRUD routes) since the original
schema, but nothing ever referenced it — users had no way to hold a role, so
authorization had no data model to build on. This adds that link and seeds
the roles the API will actually distinguish between.

Deliberately behaviour-neutral: role_id is nullable, no existing row is
backfilled, and UserResponse declares its fields explicitly so the new
column does not appear in any API response. Assigning roles to real users is
a separate, deliberate step.
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c1f7a92e4d68'
down_revision: Union[str, None] = 'a8c3e5b71f42'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Guarded so a create_all() that got there first can't make this fail —
    # the same race that left trading_breadth_readings without its default.
    op.execute("""
        ALTER TABLE users
        ADD COLUMN IF NOT EXISTS role_id INTEGER REFERENCES roles(id)
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_users_role_id ON users (role_id)
    """)

    # Seed the three roles the API will distinguish. ON CONFLICT keeps this
    # idempotent and preserves any description already written by hand.
    # 'trader' is separate from 'admin' on purpose: the person who should
    # manage customer records is not necessarily the person who should be
    # able to start the trading scheduler or flip the kill switch.
    op.execute("""
        INSERT INTO roles (role, description) VALUES
            ('admin',  'Full access, including destructive writes and user management'),
            ('trader', 'Trading engine controls: scheduler, manual cycles, kill switch'),
            ('user',   'Read access to business records')
        ON CONFLICT (role) DO NOTHING
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_users_role_id")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS role_id")
    op.execute("DELETE FROM roles WHERE role IN ('admin', 'trader', 'user')")
