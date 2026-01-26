"""Facade module for `from zerg.crud import crud` pattern.

This module re-exports all CRUD functions so that the old import pattern:
    from zerg.crud import crud
    crud.get_user(db, user_id)

continues to work after splitting the monolithic crud.py into domain-specific files.

For new code, prefer direct imports from the domain modules.
"""

# Re-export model classes for backwards compatibility
from zerg.models import CanvasLayout  # noqa: F401
from zerg.models import CommisJob  # noqa: F401
from zerg.models import Connector  # noqa: F401
from zerg.models import Course  # noqa: F401
from zerg.models import Fiche  # noqa: F401
from zerg.models import FicheMessage  # noqa: F401
from zerg.models import Thread  # noqa: F401
from zerg.models import ThreadMessage  # noqa: F401
from zerg.models import Trigger  # noqa: F401
from zerg.models import User  # noqa: F401
from zerg.models import Workflow  # noqa: F401
from zerg.models import WorkflowExecution  # noqa: F401
from zerg.models import WorkflowTemplate  # noqa: F401

# Re-export all CRUD functions from the package (single source of truth in __init__.py)
from . import *  # noqa: F401, F403
