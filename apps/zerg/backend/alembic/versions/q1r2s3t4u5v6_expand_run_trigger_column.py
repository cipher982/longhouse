"""expand_run_trigger_column

Revision ID: q1r2s3t4u5v6
Revises: p0q1r2s3t4u5
Create Date: 2026-01-10 22:45:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'q1r2s3t4u5v6'
down_revision: Union[str, Sequence[str], None] = 'p0q1r2s3t4u5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Expand trigger column to 50 chars to accommodate 'continuation' (12 chars)
    # and future trigger types
    op.alter_column('agent_runs', 'trigger',
               existing_type=sa.String(length=8),
               type_=sa.String(length=50),
               existing_nullable=False)


def downgrade() -> None:
    # We can't safely shrink it back if there are long values
    pass
