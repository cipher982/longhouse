"""Vulture whitelist for intentionally unused code.

This file whitelists code that vulture flags as unused but is actually:
- Required by framework signatures (SQLAlchemy events, FastAPI dependencies)
- Exception handler variables that are intentionally unused
- Test fixtures required by pytest but not explicitly referenced
- TYPE_CHECKING imports used only in type annotations
"""

# TYPE_CHECKING imports used in string type annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from langgraph.types import RunnableConfig

    # Dummy usage to whitelist TYPE_CHECKING imports
    _ = RunnableConfig

# SQLAlchemy event handlers require connection_record in signature
connection_record = None

# FastAPI exception handlers require __context parameter
__context = None

# Exception handling variables (standard Python practice to assign all 3)
exc_type = None
exc_val = None
exc_tb = None

# Pytest fixtures in test files need these parameters even if unused
force_macos = None
src = None
dst = None
follow_symlinks = None
