"""add entity_images gallery table

Revision ID: a3f1c9d02b47
Revises: 955b7cd7708a
Create Date: 2026-08-16 17:30:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a3f1c9d02b47'
down_revision: Union[str, None] = '955b7cd7708a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Supports multiple images per entity record (e.g. several product/property
    # photos), on top of the existing single image_url column on each entity
    # table. No FK on entity_id — entity_type is polymorphic across several
    # tables, so a single FK can't target all of them.
    op.execute("""
        CREATE TABLE IF NOT EXISTS entity_images (
            id SERIAL PRIMARY KEY,
            entity_type VARCHAR NOT NULL,
            entity_id INTEGER NOT NULL,
            image_url VARCHAR NOT NULL,
            sort_order INTEGER DEFAULT 0,
            created_at TIMESTAMPTZ DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_entity_images_entity_type ON entity_images (entity_type)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_entity_images_entity_id ON entity_images (entity_id)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS entity_images")
