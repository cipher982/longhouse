"""add_connector_credentials_table

Revision ID: e5f6g7h8i9j0
Revises: 74a690bf8231
Create Date: 2025-11-29 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e5f6g7h8i9j0'
down_revision: Union[str, Sequence[str], None] = '74a690bf8231'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create connector_credentials table for fiche-scoped tool credentials."""
    # Check if table already exists (may have been created by schema init)
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if 'connector_credentials' in inspector.get_table_names():
        print("connector_credentials table already exists - skipping")
        return

    op.create_table(
        'connector_credentials',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('fiche_id', sa.Integer(), sa.ForeignKey('fiches.id', ondelete='CASCADE'), nullable=False, index=True),
        sa.Column('connector_type', sa.String(50), nullable=False),
        sa.Column('encrypted_value', sa.Text(), nullable=False),
        sa.Column('display_name', sa.String(255), nullable=True),
        sa.Column('connector_metadata', sa.JSON(), nullable=True),
        sa.Column('test_status', sa.String(20), nullable=False, server_default='untested'),
        sa.Column('last_tested_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False),
    )

    # Add unique constraint on (fiche_id, connector_type)
    op.create_unique_constraint(
        'uix_fiche_connector',
        'connector_credentials',
        ['fiche_id', 'connector_type']
    )


def downgrade() -> None:
    """Drop connector_credentials table."""
    op.drop_constraint('uix_fiche_connector', 'connector_credentials', type_='unique')
    op.drop_table('connector_credentials')
