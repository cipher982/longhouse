"""Add unique constraint on agent (owner_id, name)

Revision ID: c3d4e5f6g7h8
Revises: b2c3d4e5f6g7
Create Date: 2025-11-10 17:45:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c3d4e5f6g7h8'
down_revision = 'b2c3d4e5f6g7'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Check if constraint already exists (idempotent)
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    # Check if constraint already exists
    existing_constraints = [c['name'] for c in inspector.get_unique_constraints('agents')]
    if 'uq_agent_owner_name' in existing_constraints:
        print("uq_agent_owner_name constraint already exists - skipping")
        return

    # Find duplicate agent IDs to delete (keep the highest ID for each owner_id, name pair)
    result = conn.execute(sa.text("""
        SELECT id FROM agents
        WHERE id NOT IN (
            SELECT MAX(id)
            FROM agents
            GROUP BY owner_id, name
        )
    """))
    duplicate_ids = [row[0] for row in result.fetchall()]

    if duplicate_ids:
        print(f"Cleaning up {len(duplicate_ids)} duplicate agents: {duplicate_ids}")

        # Delete related records first (cascade manually to avoid FK violations)
        for table in ['agent_threads', 'agent_runs', 'agent_messages', 'threads', 'worker_jobs']:
            try:
                conn.execute(sa.text(f"DELETE FROM {table} WHERE agent_id = ANY(:ids)"), {"ids": duplicate_ids})
            except Exception:
                pass  # Table might not exist or have agent_id column

        # Now delete the duplicate agents
        conn.execute(sa.text("DELETE FROM agents WHERE id = ANY(:ids)"), {"ids": duplicate_ids})

    # Now create unique constraint on (owner_id, name)
    op.create_unique_constraint('uq_agent_owner_name', 'agents', ['owner_id', 'name'])


def downgrade() -> None:
    # Drop the unique constraint
    op.drop_constraint('uq_agent_owner_name', 'agents', type_='unique')
