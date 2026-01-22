"""Add user contacts and rate limiting tables.

Revision ID: y2z3a4b5c6d7
Revises: x1y2z3a4b5c6
Create Date: 2026-01-21

Approved contacts system for external action tools (email, SMS).
Prevents abuse while keeping the platform usable.
"""

from typing import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "y2z3a4b5c6d7"
down_revision: Union[str, Sequence[str], None] = "x1y2z3a4b5c6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "zerg"


def upgrade() -> None:
    """Create user contacts and rate limiting tables."""
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    existing_tables = inspector.get_table_names(schema=SCHEMA)

    # Email contacts (stores both display and normalized for matching)
    if "user_email_contacts" not in existing_tables:
        op.create_table(
            "user_email_contacts",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("owner_id", sa.Integer(), nullable=False),
            sa.Column("name", sa.String(length=100), nullable=False),
            sa.Column("email", sa.String(length=255), nullable=False),
            sa.Column("email_normalized", sa.String(length=255), nullable=False),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
            sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
            sa.ForeignKeyConstraint(["owner_id"], [f"{SCHEMA}.users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("owner_id", "email_normalized", name="uq_email_contact_owner_email"),
            schema=SCHEMA,
        )
        op.create_index(
            "ix_user_email_contacts_owner_id",
            "user_email_contacts",
            ["owner_id"],
            schema=SCHEMA,
        )

    # Phone contacts (E.164 normalized)
    if "user_phone_contacts" not in existing_tables:
        op.create_table(
            "user_phone_contacts",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("owner_id", sa.Integer(), nullable=False),
            sa.Column("name", sa.String(length=100), nullable=False),
            sa.Column("phone", sa.String(length=20), nullable=False),
            sa.Column("phone_normalized", sa.String(length=20), nullable=False),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
            sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
            sa.ForeignKeyConstraint(["owner_id"], [f"{SCHEMA}.users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("owner_id", "phone_normalized", name="uq_phone_contact_owner_phone"),
            schema=SCHEMA,
        )
        op.create_index(
            "ix_user_phone_contacts_owner_id",
            "user_phone_contacts",
            ["owner_id"],
            schema=SCHEMA,
        )

    # Daily email rate limit counter (atomic, prevents race conditions)
    if "user_daily_email_counter" not in existing_tables:
        op.create_table(
            "user_daily_email_counter",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("date", sa.Date(), nullable=False),
            sa.Column("count", sa.Integer(), nullable=False, server_default="0"),
            sa.ForeignKeyConstraint(["user_id"], [f"{SCHEMA}.users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("user_id", "date", name="uq_email_counter_user_date"),
            schema=SCHEMA,
        )

    # Daily SMS rate limit counter
    if "user_daily_sms_counter" not in existing_tables:
        op.create_table(
            "user_daily_sms_counter",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("date", sa.Date(), nullable=False),
            sa.Column("count", sa.Integer(), nullable=False, server_default="0"),
            sa.ForeignKeyConstraint(["user_id"], [f"{SCHEMA}.users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("user_id", "date", name="uq_sms_counter_user_date"),
            schema=SCHEMA,
        )

    # Email send audit log (for debugging/compliance)
    if "email_send_log" not in existing_tables:
        op.create_table(
            "email_send_log",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("to_email", sa.String(length=255), nullable=False),
            sa.Column("sent_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
            sa.ForeignKeyConstraint(["user_id"], [f"{SCHEMA}.users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            schema=SCHEMA,
        )
        op.create_index(
            "ix_email_send_log_user_sent",
            "email_send_log",
            ["user_id", "sent_at"],
            schema=SCHEMA,
        )


def downgrade() -> None:
    """Drop user contacts and rate limiting tables."""
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    existing_tables = inspector.get_table_names(schema=SCHEMA)

    if "email_send_log" in existing_tables:
        op.drop_index("ix_email_send_log_user_sent", table_name="email_send_log", schema=SCHEMA)
        op.drop_table("email_send_log", schema=SCHEMA)

    if "user_daily_sms_counter" in existing_tables:
        op.drop_table("user_daily_sms_counter", schema=SCHEMA)

    if "user_daily_email_counter" in existing_tables:
        op.drop_table("user_daily_email_counter", schema=SCHEMA)

    if "user_phone_contacts" in existing_tables:
        op.drop_index("ix_user_phone_contacts_owner_id", table_name="user_phone_contacts", schema=SCHEMA)
        op.drop_table("user_phone_contacts", schema=SCHEMA)

    if "user_email_contacts" in existing_tables:
        op.drop_index("ix_user_email_contacts_owner_id", table_name="user_email_contacts", schema=SCHEMA)
        op.drop_table("user_email_contacts", schema=SCHEMA)
