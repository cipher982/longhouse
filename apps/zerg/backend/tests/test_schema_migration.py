"""Schema migration safety tests.

Verifies that initialize_database() properly adds all model columns to an
existing sessions table that only has the original schema. Catches bugs like
the reflected_at column being defined on the model but missing the ALTER TABLE
migration in _migrate_agents_columns().

Uses in-memory SQLite with raw DDL to simulate a pre-existing database.
"""

from sqlalchemy import inspect, text

from zerg.database import initialize_database, make_engine
from zerg.models.agents import AgentSession

# The original sessions DDL — only base columns, no summary/needs_embedding/reflected_at.
# This simulates a database created before those columns were added.
ORIGINAL_SESSIONS_DDL = """\
CREATE TABLE sessions (
    id CHAR(36) PRIMARY KEY,
    provider VARCHAR(50) NOT NULL,
    environment VARCHAR(20) NOT NULL,
    project VARCHAR(255),
    device_id VARCHAR(255),
    cwd TEXT,
    git_repo VARCHAR(500),
    git_branch VARCHAR(255),
    started_at DATETIME NOT NULL,
    ended_at DATETIME,
    user_messages INTEGER DEFAULT 0,
    assistant_messages INTEGER DEFAULT 0,
    tool_calls INTEGER DEFAULT 0,
    provider_session_id VARCHAR(255),
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
)"""


def _make_old_db():
    """Create an in-memory SQLite engine with only the original sessions schema."""
    engine = make_engine("sqlite://")
    with engine.connect() as conn:
        conn.execute(text(ORIGINAL_SESSIONS_DDL))
        conn.commit()
    return engine


def test_migration_adds_all_model_columns():
    """Every column on AgentSession must exist after initialize_database() runs
    against a database with only the original sessions schema.

    If this test fails, you need to add an ALTER TABLE statement to
    _migrate_agents_columns() in database.py.
    """
    engine = _make_old_db()
    initialize_database(engine)

    inspector = inspect(engine)
    actual_columns = {col["name"] for col in inspector.get_columns("sessions")}

    # Get all column names from the SQLAlchemy model
    model_columns = {col.name for col in AgentSession.__table__.columns}

    missing = model_columns - actual_columns
    assert not missing, (
        f"AgentSession has columns not present after migration: {missing}. "
        "Add ALTER TABLE to _migrate_agents_columns()."
    )


def test_migration_is_idempotent():
    """Running initialize_database() twice must not raise errors."""
    engine = _make_old_db()
    initialize_database(engine)
    initialize_database(engine)  # second run — must be a no-op

    inspector = inspect(engine)
    actual_columns = {col["name"] for col in inspector.get_columns("sessions")}
    model_columns = {col.name for col in AgentSession.__table__.columns}

    missing = model_columns - actual_columns
    assert not missing, f"Columns missing after second init: {missing}"
