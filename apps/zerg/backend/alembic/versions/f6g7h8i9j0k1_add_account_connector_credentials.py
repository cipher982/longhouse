"""add_account_connector_credentials

Account-level connector credentials for built-in tools.
Enables users to configure credentials once and share across all agents.
Agents can still override with per-agent credentials.

Revision ID: f6g7h8i9j0k1
Revises: e5f6g7h8i9j0
Create Date: 2025-11-30 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f6g7h8i9j0k1'
down_revision: Union[str, Sequence[str], None] = 'e5f6g7h8i9j0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create account_connector_credentials table for account-scoped tool credentials."""
    # Check if table already exists (may have been created by schema init)
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if 'account_connector_credentials' in inspector.get_table_names():
        print("account_connector_credentials table already exists - skipping")
        return

    op.create_table(
        'account_connector_credentials',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('owner_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True),
        sa.Column('organization_id', sa.Integer(), nullable=True, index=True),  # Reserved for future org support
        sa.Column('connector_type', sa.String(50), nullable=False),
        sa.Column('encrypted_value', sa.Text(), nullable=False),
        sa.Column('display_name', sa.String(255), nullable=True),
        sa.Column('connector_metadata', sa.JSON(), nullable=True),
        sa.Column('test_status', sa.String(20), nullable=False, server_default='untested'),
        sa.Column('last_tested_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False),
    )

    # Add unique constraint on (owner_id, connector_type)
    op.create_unique_constraint(
        'uix_account_owner_connector',
        'account_connector_credentials',
        ['owner_id', 'connector_type']
    )


def downgrade() -> None:
    """Drop account_connector_credentials table."""
    op.drop_constraint('uix_account_owner_connector', 'account_connector_credentials', type_='unique')
    op.drop_table('account_connector_credentials')
