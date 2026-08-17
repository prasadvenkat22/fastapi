"""seed the two admin accounts and make email a unique login identifier

Revision ID: d4b8e13c7a95
Revises: c1f7a92e4d68
Create Date: 2026-08-17 00:00:00.000000

Creates the first two admin accounts. Under the agreed role model, admin is
the full-access role and includes the trading routes; 'trader' stays a
lesser role for someone who should only be able to drive the engine and not
delete business records.

Both rows are created WITHOUT a password. Nothing can authenticate yet (no
login route, no JWT library), and a password invented here would either be
known to no one or be a shared secret sitting in version control. The
set-password step lands with the login endpoint.

Also promotes email to a unique constraint. It is about to become the login
identifier, and two rows sharing one email would make authentication
ambiguous -- register_user already assumes uniqueness but only enforces it
with a race-prone SELECT-then-INSERT.
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'd4b8e13c7a95'
down_revision: Union[str, None] = 'c1f7a92e4d68'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

ADMIN_EMAILS = ("venkatangirala@gmail.com", "prasadvenkat@hotmail.com")


def upgrade() -> None:
    # Verified no duplicate emails exist before adding this; the constraint
    # would fail loudly rather than silently if that ever stopped being true.
    op.execute("""
        ALTER TABLE users ADD CONSTRAINT uq_users_email UNIQUE (email)
    """)

    # Admin now explicitly covers trading, so the seeded description should
    # say so rather than leaving the boundary to interpretation.
    op.execute("""
        UPDATE roles
        SET description = 'Full access: business records, users, GenAI, and the trading engine'
        WHERE role = 'admin'
    """)

    # WHERE NOT EXISTS rather than ON CONFLICT: the unique constraint above
    # is created in this same migration, and keeping the guard explicit means
    # this still reads correctly if the constraint is ever dropped.
    for email in ADMIN_EMAILS:
        op.execute(f"""
            INSERT INTO users (name, email, password_hash, disabled, role_id)
            SELECT '{email.split('@')[0]}', '{email}', NULL, false,
                   (SELECT id FROM roles WHERE role = 'admin')
            WHERE NOT EXISTS (SELECT 1 FROM users WHERE email = '{email}')
        """)


def downgrade() -> None:
    for email in ADMIN_EMAILS:
        op.execute(f"DELETE FROM users WHERE email = '{email}'")
    op.execute("ALTER TABLE users DROP CONSTRAINT IF EXISTS uq_users_email")
