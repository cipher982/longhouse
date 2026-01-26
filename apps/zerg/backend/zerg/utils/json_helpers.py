"""JSON field helper utilities for SQLAlchemy models.

This module provides utilities for safely updating JSON fields in SQLAlchemy
models, ensuring proper change tracking and avoiding common pitfalls.
"""

from typing import Any
from typing import Dict
from typing import Optional

from sqlalchemy.orm.attributes import flag_modified


def update_json_field(model: Any, field_name: str, updates: Dict[str, Any]) -> None:
    """Safely update a JSON field ensuring SQLAlchemy tracks changes.

    This helper merges the updates into the existing JSON field value and
    ensures SQLAlchemy knows the field has been modified. If using MutableDict,
    the flag_modified call is not necessary but doesn't hurt.

    Args:
        model: The SQLAlchemy model instance
        field_name: The name of the JSON field to update
        updates: Dictionary of updates to merge into the field

    Example:
        >>> fiche = db.query(Fiche).first()
        >>> update_json_field(fiche, 'config', {'new_setting': 'value'})
        >>> db.commit()
    """
    current = getattr(model, field_name) or {}
    new_value = {**current, **updates}
    setattr(model, field_name, new_value)

    # This is now redundant with MutableDict but kept for compatibility
    # with any models that haven't been migrated yet
    flag_modified(model, field_name)


def set_json_field(model: Any, field_name: str, value: Optional[Dict[str, Any]]) -> None:
    """Replace the entire JSON field value.

    Args:
        model: The SQLAlchemy model instance
        field_name: The name of the JSON field to set
        value: The new value for the field (can be None)

    Example:
        >>> fiche = db.query(Fiche).first()
        >>> set_json_field(fiche, 'config', {'completely': 'new'})
        >>> db.commit()
    """
    setattr(model, field_name, value)

    # This is now redundant with MutableDict but kept for compatibility
    flag_modified(model, field_name)


def get_json_value(model: Any, field_name: str, key: str, default: Any = None) -> Any:
    """Safely get a value from a JSON field.

    Args:
        model: The SQLAlchemy model instance
        field_name: The name of the JSON field
        key: The key to look up in the JSON field
        default: Default value if key is not found

    Returns:
        The value at the key, or default if not found

    Example:
        >>> fiche = db.query(Fiche).first()
        >>> timeout = get_json_value(fiche, 'config', 'timeout', 30)
    """
    field_value = getattr(model, field_name)
    if not isinstance(field_value, dict):
        return default
    return field_value.get(key, default)


def remove_json_key(model: Any, field_name: str, key: str) -> bool:
    """Remove a key from a JSON field.

    Args:
        model: The SQLAlchemy model instance
        field_name: The name of the JSON field
        key: The key to remove from the JSON field

    Returns:
        True if the key was removed, False if it didn't exist

    Example:
        >>> fiche = db.query(Fiche).first()
        >>> removed = remove_json_key(fiche, 'config', 'old_setting')
        >>> db.commit()
    """
    field_value = getattr(model, field_name)
    if not isinstance(field_value, dict) or key not in field_value:
        return False

    # Create a new dict without the key
    new_value = {k: v for k, v in field_value.items() if k != key}
    setattr(model, field_name, new_value)
    flag_modified(model, field_name)
    return True
