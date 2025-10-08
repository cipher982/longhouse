"""Initial schema baseline

Revision ID: 458f9a6a8779
Revises:
Create Date: 2025-09-12 11:28:10.221762

"""

from typing import Sequence
from typing import Union

# revision identifiers, used by Alembic.
revision: str = "458f9a6a8779"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Initial schema baseline - no-op since tables already exist."""
    # This is a baseline migration for existing databases.
    # Tables were created via SQLAlchemy create_all() or admin reset.
    pass


def downgrade() -> None:
    """Cannot downgrade from baseline."""
    # Cannot downgrade from the initial baseline
    raise NotImplementedError("Cannot downgrade from initial baseline")
