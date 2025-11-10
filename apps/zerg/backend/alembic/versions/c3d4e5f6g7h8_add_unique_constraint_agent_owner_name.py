"""Add unique constraint on agent (owner_id, name)

Revision ID: c3d4e5f6g7h8
Revises: b2c3d4e5f6g7
Create Date: 2025-11-10 17:45:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c3d4e5f6g7h8'
down_revision = 'b2c3d4e5f6g7'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create unique constraint on (owner_id, name)
    # This prevents a user from having multiple agents with the same name
    # Different users can still have agents with the same name
    op.create_unique_constraint('uq_agent_owner_name', 'agents', ['owner_id', 'name'])


def downgrade() -> None:
    # Drop the unique constraint
    op.drop_constraint('uq_agent_owner_name', 'agents', type_='unique')
