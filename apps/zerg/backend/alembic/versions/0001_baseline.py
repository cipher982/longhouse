"""Baseline schema snapshot.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-01-28

This is a squashed baseline migration. Database schema is created via
SQLAlchemy create_all() on startup; Alembic tracks version only.
"""
from typing import Sequence, Union

# revision identifiers, used by Alembic.
revision: str = "0001_baseline"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Baseline no-op migration."""
    pass


def downgrade() -> None:
    raise NotImplementedError("Cannot downgrade from baseline")
