"""registrations: status and notes

The Pydantic schema (RegistrationBase) and the site's Demo Registrations page
have carried `status` and `notes` since the CRM UI was written, but the ORM
model and the table never did -- the CRUD route dropped both on the way in
(model_dump(include=_fields)). The public contact form (section 202) needs
somewhere to keep the visitor's message, and the admin page already renders
`notes` and colours by `status`, so the columns finally exist.

Revision ID: a9c4e17b52d3
Revises: d1e7c9a24f80
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a9c4e17b52d3"
down_revision: Union[str, None] = "d1e7c9a24f80"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_COLUMNS = (("status", "VARCHAR DEFAULT 'requested'"), ("notes", "TEXT"))


def _table_missing() -> bool:
    """Fresh-database guard, like every other revision here: create_all in
    main.py builds the full table on first boot and this has nothing to add."""
    return not sa.inspect(op.get_bind()).has_table("registrations")


def upgrade() -> None:
    if _table_missing():
        return
    for name, sqltype in _COLUMNS:
        op.execute(f"ALTER TABLE registrations ADD COLUMN IF NOT EXISTS {name} {sqltype}")


def downgrade() -> None:
    if _table_missing():
        return
    for name, _ in _COLUMNS:
        op.execute(f"ALTER TABLE registrations DROP COLUMN IF EXISTS {name}")
