"""Add device_tokens table for per-device authentication.

Revision ID: 0004_device_tokens
Revises: 0003_raw_json_provider_session_id
Create Date: 2026-01-29

Adds the device_tokens table used by the shipper and device token API.
"""

from typing import Sequence
from typing import Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "0004_device_tokens"
down_revision: Union[str, Sequence[str], None] = "0003_raw_json_provider_session_id"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create device_tokens table."""
    bind = op.get_bind()
    inspector = inspect(bind)
    existing_tables = set(inspector.get_table_names(schema="zerg"))

    if "device_tokens" not in existing_tables:
        op.create_table(
            "device_tokens",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("owner_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("device_id", sa.String(255), nullable=False),
            sa.Column("token_hash", sa.String(64), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),
            sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        )

    existing_indexes = {idx["name"] for idx in inspector.get_indexes("device_tokens", schema="zerg")}
    existing_uniques = {uc["name"] for uc in inspector.get_unique_constraints("device_tokens", schema="zerg")}

    if "ix_device_tokens_owner_id" not in existing_indexes:
        op.create_index("ix_device_tokens_owner_id", "device_tokens", ["owner_id"])
    if "ix_device_tokens_token_hash" not in existing_indexes and "ix_device_tokens_token_hash" not in existing_uniques:
        op.create_index("ix_device_tokens_token_hash", "device_tokens", ["token_hash"], unique=True)
    if "ix_device_tokens_owner_device" not in existing_indexes:
        op.create_index("ix_device_tokens_owner_device", "device_tokens", ["owner_id", "device_id"])


def downgrade() -> None:
    """Drop device_tokens table."""
    op.drop_index("ix_device_tokens_owner_device", table_name="device_tokens")
    op.drop_index("ix_device_tokens_token_hash", table_name="device_tokens")
    op.drop_index("ix_device_tokens_owner_id", table_name="device_tokens")
    op.drop_table("device_tokens")
