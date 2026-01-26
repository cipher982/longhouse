"""add model to courses

Revision ID: p0q1r2s3t4u5
Revises: o9p0q1r2s3t4
Create Date: 2026-01-10 12:00:00.000000

"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "p0q1r2s3t4u5"
down_revision = "o9p0q1r2s3t4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add model column to courses for continuation model inheritance
    # When a continuation run is created, it inherits the model from the original run
    # This is critical for gpt-scripted tests and consistent model usage
    conn = op.get_bind()

    # Check if column exists
    result = conn.execute(
        sa.text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'courses' AND column_name = 'model'"
        )
    )
    if result.fetchone() is None:
        op.add_column(
            "courses",
            sa.Column("model", sa.String(100), nullable=True),
        )


def downgrade() -> None:
    op.drop_column("courses", "model")
