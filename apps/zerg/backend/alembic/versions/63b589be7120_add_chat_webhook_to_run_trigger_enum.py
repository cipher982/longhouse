"""add_chat_webhook_to_run_trigger_enum

Revision ID: 63b589be7120
Revises: d4e5f6g7h8i9
Create Date: 2025-11-11 19:31:13.212296

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '63b589be7120'
down_revision: Union[str, Sequence[str], None] = 'd4e5f6g7h8i9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add 'chat' and 'webhook' values to run_trigger_enum.
    
    Note: The model uses native_enum=False, so this is stored as VARCHAR in PostgreSQL,
    not as a native ENUM type. No ALTER TYPE needed - the values are just strings.
    """
    # The model uses SAEnum(..., native_enum=False), which stores as VARCHAR
    # No database-level enum type exists, so no ALTER TYPE is needed.
    # New values can be inserted directly as strings.
    pass


def downgrade() -> None:
    """Remove 'chat' and 'webhook' from run_trigger_enum.

    Note: PostgreSQL doesn't support removing enum values directly.
    This would require recreating the enum type, which is complex and risky.
    For safety, we leave the enum values in place on downgrade.
    """
    # PostgreSQL doesn't support removing enum values
    # Downgrade is a no-op to avoid data loss
    pass
