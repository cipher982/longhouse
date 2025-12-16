"""add_knowledge_base_tables

Revision ID: 35e451933c7a
Revises: ecf3e5aa2219
Create Date: 2025-12-16 16:40:57.374105

Add knowledge_sources and knowledge_documents tables for Phase 0 of
Knowledge Sync feature.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSON


# revision identifiers, used by Alembic.
revision: str = '35e451933c7a'
down_revision: Union[str, Sequence[str], None] = 'ecf3e5aa2219'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create knowledge_sources and knowledge_documents tables."""
    # Create knowledge_sources table
    op.create_table(
        'knowledge_sources',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('owner_id', sa.Integer(), nullable=False),
        sa.Column('source_type', sa.String(length=50), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('config', JSON, nullable=False),
        sa.Column('sync_schedule', sa.String(length=100), nullable=True),
        sa.Column('last_synced_at', sa.DateTime(), nullable=True),
        sa.Column('sync_status', sa.String(length=50), nullable=False, server_default='pending'),
        sa.Column('sync_error', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['owner_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_knowledge_sources_id'), 'knowledge_sources', ['id'], unique=False)
    op.create_index(op.f('ix_knowledge_sources_owner_id'), 'knowledge_sources', ['owner_id'], unique=False)

    # Create knowledge_documents table
    op.create_table(
        'knowledge_documents',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('source_id', sa.Integer(), nullable=False),
        sa.Column('owner_id', sa.Integer(), nullable=False),
        sa.Column('path', sa.String(length=1024), nullable=False),
        sa.Column('title', sa.String(length=512), nullable=True),
        sa.Column('content_text', sa.Text(), nullable=False),
        sa.Column('content_hash', sa.String(length=64), nullable=False),
        sa.Column('doc_metadata', JSON, nullable=True),
        sa.Column('fetched_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['owner_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['source_id'], ['knowledge_sources.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('source_id', 'path', name='uq_source_path')
    )
    op.create_index(op.f('ix_knowledge_documents_id'), 'knowledge_documents', ['id'], unique=False)
    op.create_index(op.f('ix_knowledge_documents_owner_id'), 'knowledge_documents', ['owner_id'], unique=False)
    op.create_index(op.f('ix_knowledge_documents_source_id'), 'knowledge_documents', ['source_id'], unique=False)


def downgrade() -> None:
    """Drop knowledge_documents and knowledge_sources tables."""
    op.drop_index(op.f('ix_knowledge_documents_source_id'), table_name='knowledge_documents')
    op.drop_index(op.f('ix_knowledge_documents_owner_id'), table_name='knowledge_documents')
    op.drop_index(op.f('ix_knowledge_documents_id'), table_name='knowledge_documents')
    op.drop_table('knowledge_documents')

    op.drop_index(op.f('ix_knowledge_sources_owner_id'), table_name='knowledge_sources')
    op.drop_index(op.f('ix_knowledge_sources_id'), table_name='knowledge_sources')
    op.drop_table('knowledge_sources')
