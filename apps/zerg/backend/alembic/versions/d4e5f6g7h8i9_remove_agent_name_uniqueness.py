"""Remove unique constraint on agent (owner_id, name)

Revision ID: d4e5f6g7h8i9
Revises: c3d4e5f6g7h8
Create Date: 2025-11-10 18:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd4e5f6g7h8i9'
down_revision = 'c3d4e5f6g7h8'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Drop the unique constraint on (owner_id, name)
    # This allows multiple agents with the same name (e.g., "New Agent")
    op.drop_constraint('uq_agent_owner_name', 'agents', type_='unique')


def downgrade() -> None:
    # Recreate the unique constraint
    op.create_unique_constraint('uq_agent_owner_name', 'agents', ['owner_id', 'name'])
