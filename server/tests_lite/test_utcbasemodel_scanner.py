"""Scanner test that validates all Pydantic response models with datetime fields inherit UTCBaseModel.

This test introspects the codebase to catch any response models that were missed
during the UTC serialization migration or added later without proper datetime handling.
"""

import inspect
import pkgutil
from datetime import datetime
from typing import get_args, get_origin

import pytest
from pydantic import BaseModel

from zerg.utils.time import UTCBaseModel


def _has_datetime_field(model_class):
    """Check if a Pydantic model has any datetime fields."""
    if not hasattr(model_class, "model_fields"):
        return False

    for field_name, field_info in model_class.model_fields.items():
        annotation = field_info.annotation

        # Direct datetime
        if annotation is datetime:
            return True

        # Optional[datetime] or Union[datetime, None]
        if get_origin(annotation) is type(None) or hasattr(annotation, "__args__"):
            args = get_args(annotation)
            if datetime in args:
                return True

    return False


def test_response_models_with_datetime_inherit_utcbasemodel():
    """All Pydantic response models with datetime fields must inherit from UTCBaseModel."""
    import zerg.routers as routers_pkg
    import zerg.schemas as schemas_pkg

    # Collect all modules to scan
    modules_to_scan = []

    # Scan routers package
    for importer, modname, ispkg in pkgutil.walk_packages(
        path=routers_pkg.__path__,
        prefix="zerg.routers.",
    ):
        modules_to_scan.append(modname)

    # Scan schemas package
    for importer, modname, ispkg in pkgutil.walk_packages(
        path=schemas_pkg.__path__,
        prefix="zerg.schemas.",
    ):
        modules_to_scan.append(modname)

    violations = []

    for module_name in modules_to_scan:
        try:
            module = __import__(module_name, fromlist=[""])
        except Exception as e:
            # Skip modules that fail to import (likely due to missing deps in test env)
            continue

        # Find all Pydantic models in the module
        for name, obj in inspect.getmembers(module, inspect.isclass):
            # Must be a Pydantic model
            if not issubclass(obj, BaseModel):
                continue

            # Skip base classes themselves
            if obj in (BaseModel, UTCBaseModel):
                continue

            # Skip models defined in other modules (imports)
            if obj.__module__ != module_name:
                continue

            # Skip request models (Create, Update, Patch) - they're input, not output
            if any(suffix in name for suffix in ("Create", "Update", "Patch", "Request", "Input")):
                continue

            # Check if model has datetime fields
            if not _has_datetime_field(obj):
                continue

            # Model has datetime fields - must inherit from UTCBaseModel
            if not issubclass(obj, UTCBaseModel):
                violations.append(f"{obj.__module__}.{obj.__name__}")

    if violations:
        msg = "Found Pydantic models with datetime fields not inheriting from UTCBaseModel:\n\n"
        for v in sorted(violations):
            msg += f"  - {v}\n"
        msg += "\nThese models will serialize naive datetimes without 'Z' suffix, "
        msg += "causing JavaScript clients to parse them as local time.\n"
        msg += "Fix by changing `class Foo(BaseModel)` to `class Foo(UTCBaseModel)`."
        pytest.fail(msg)


if __name__ == "__main__":
    # Allow running directly for development
    test_response_models_with_datetime_inherit_utcbasemodel()
    print("✓ All response models with datetime fields properly inherit UTCBaseModel")
