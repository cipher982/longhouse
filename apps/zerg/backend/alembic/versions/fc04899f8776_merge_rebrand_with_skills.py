"""merge_rebrand_with_skills

Revision ID: fc04899f8776
Revises: d7e8f9g0h1i2, f1a2b3c4d5e6
Create Date: 2026-01-27 15:46:28.678848

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'fc04899f8776'
down_revision: Union[str, Sequence[str], None] = ('d7e8f9g0h1i2', 'f1a2b3c4d5e6')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
