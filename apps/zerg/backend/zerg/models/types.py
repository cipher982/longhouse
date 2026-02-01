"""Shared SQLAlchemy type decorators for SQLite."""

from uuid import UUID as PyUUID

from sqlalchemy.types import CHAR
from sqlalchemy.types import TypeDecorator


class GUID(TypeDecorator):
    """GUID type stored as CHAR(36) in SQLite.

    Converts Python UUID objects to/from string representation for storage.
    """

    impl = CHAR(36)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return value
        return str(value) if isinstance(value, PyUUID) else value

    def process_result_value(self, value, dialect):
        if value is None:
            return value
        elif isinstance(value, PyUUID):
            return value
        else:
            return PyUUID(value)
