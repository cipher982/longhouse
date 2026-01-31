"""Shared SQLAlchemy type decorators for cross-database compatibility."""

from uuid import UUID as PyUUID

from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.types import CHAR
from sqlalchemy.types import TypeDecorator


class GUID(TypeDecorator):
    """Platform-independent GUID type.

    Uses PostgreSQL's UUID type for Postgres, stores as CHAR(36) for SQLite.
    Based on SQLAlchemy's TypeDecorator pattern for cross-database UUID support.
    """

    impl = CHAR
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(PG_UUID(as_uuid=True))
        else:
            return dialect.type_descriptor(CHAR(36))

    def process_bind_param(self, value, dialect):
        if value is None:
            return value
        elif dialect.name == "postgresql":
            return value if isinstance(value, PyUUID) else PyUUID(value)
        else:
            return str(value) if isinstance(value, PyUUID) else value

    def process_result_value(self, value, dialect):
        if value is None:
            return value
        elif isinstance(value, PyUUID):
            return value
        else:
            return PyUUID(value)
