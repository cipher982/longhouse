"""add_continuation_of_course_id

Revision ID: l6m7n8o9p0q1
Revises: k5l6m7n8o9p0
Create Date: 2025-12-26 00:00:00.000000

Add continuation_of_course_id column to courses for durable runs v2.2.
This links continuation runs to their original deferred run.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'l6m7n8o9p0q1'
down_revision: Union[str, Sequence[str], None] = 'k5l6m7n8o9p0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add continuation_of_course_id column to courses."""
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    columns = [col['name'] for col in inspector.get_columns('courses')]

    if 'continuation_of_course_id' in columns:
        print("continuation_of_course_id column already exists - skipping")
        return

    op.add_column(
        'courses',
        sa.Column('continuation_of_course_id', sa.Integer(), nullable=True)
    )

    # Add foreign key constraint
    op.create_foreign_key(
        'fk_courses_continuation_of_course_id',
        'courses',
        'courses',
        ['continuation_of_course_id'],
        ['id']
    )


def downgrade() -> None:
    """Remove continuation_of_course_id column from courses."""
    op.drop_constraint('fk_courses_continuation_of_course_id', 'courses', type_='foreignkey')
    op.drop_column('courses', 'continuation_of_course_id')
