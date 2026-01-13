"""fix_llm_audit_log_fk_ondelete

Revision ID: 0a09e33fe6b0
Revises: 7799c1632888
Create Date: 2026-01-13 15:41:29.341073

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0a09e33fe6b0'
down_revision: Union[str, Sequence[str], None] = '7799c1632888'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Fix FK constraints to use ON DELETE SET NULL."""
    # Drop existing FK constraints
    op.drop_constraint('llm_audit_log_run_id_fkey', 'llm_audit_log', type_='foreignkey')
    op.drop_constraint('llm_audit_log_thread_id_fkey', 'llm_audit_log', type_='foreignkey')
    op.drop_constraint('llm_audit_log_owner_id_fkey', 'llm_audit_log', type_='foreignkey')

    # Recreate with ON DELETE SET NULL
    op.create_foreign_key(
        'llm_audit_log_run_id_fkey', 'llm_audit_log', 'agent_runs',
        ['run_id'], ['id'], ondelete='SET NULL'
    )
    op.create_foreign_key(
        'llm_audit_log_thread_id_fkey', 'llm_audit_log', 'agent_threads',
        ['thread_id'], ['id'], ondelete='SET NULL'
    )
    op.create_foreign_key(
        'llm_audit_log_owner_id_fkey', 'llm_audit_log', 'users',
        ['owner_id'], ['id'], ondelete='SET NULL'
    )


def downgrade() -> None:
    """Revert to original FK constraints (no ON DELETE action)."""
    op.drop_constraint('llm_audit_log_run_id_fkey', 'llm_audit_log', type_='foreignkey')
    op.drop_constraint('llm_audit_log_thread_id_fkey', 'llm_audit_log', type_='foreignkey')
    op.drop_constraint('llm_audit_log_owner_id_fkey', 'llm_audit_log', type_='foreignkey')

    op.create_foreign_key(
        'llm_audit_log_run_id_fkey', 'llm_audit_log', 'agent_runs',
        ['run_id'], ['id']
    )
    op.create_foreign_key(
        'llm_audit_log_thread_id_fkey', 'llm_audit_log', 'agent_threads',
        ['thread_id'], ['id']
    )
    op.create_foreign_key(
        'llm_audit_log_owner_id_fkey', 'llm_audit_log', 'users',
        ['owner_id'], ['id']
    )
