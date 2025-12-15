"""add_check_constraints_for_runner_status

Revision ID: ecf3e5aa2219
Revises: i3j4k5l6m7n8
Create Date: 2025-12-15 10:28:06.876832

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'ecf3e5aa2219'
down_revision: Union[str, Sequence[str], None] = 'i3j4k5l6m7n8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add CHECK constraints for status enums to prevent invalid values."""

    # Add CHECK constraint for runners.status
    op.create_check_constraint(
        'ck_runners_status',
        'runners',
        "status IN ('online', 'offline', 'revoked')"
    )

    # Add CHECK constraint for runner_jobs.status
    op.create_check_constraint(
        'ck_runner_jobs_status',
        'runner_jobs',
        "status IN ('queued', 'running', 'success', 'failed', 'timeout', 'canceled')"
    )


def downgrade() -> None:
    """Remove CHECK constraints."""
    op.drop_constraint('ck_runner_jobs_status', 'runner_jobs', type_='check')
    op.drop_constraint('ck_runners_status', 'runners', type_='check')
