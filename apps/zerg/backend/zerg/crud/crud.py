"""Backwards compatibility module for `from zerg.crud import crud` pattern.

This module re-exports all CRUD functions so that the old import pattern:
    from zerg.crud import crud
    crud.get_user(db, user_id)

continues to work after splitting the monolithic crud.py into domain-specific files.

For new code, prefer direct imports from the domain modules:
    from zerg.crud import get_user
    get_user(db, user_id)

Note: This module also re-exports model classes for backwards compatibility
with code that accessed models via `crud.ModelName`. This is not recommended
for new code - import models directly from zerg.models instead.
"""

# Re-export model classes for backwards compatibility
from zerg.models import Agent  # noqa: F401
from zerg.models import AgentMessage  # noqa: F401
from zerg.models import AgentRun  # noqa: F401
from zerg.models import CanvasLayout  # noqa: F401
from zerg.models import Connector  # noqa: F401
from zerg.models import Thread  # noqa: F401
from zerg.models import ThreadMessage  # noqa: F401
from zerg.models import Trigger  # noqa: F401
from zerg.models import User  # noqa: F401
from zerg.models import WorkerJob  # noqa: F401
from zerg.models import Workflow  # noqa: F401
from zerg.models import WorkflowExecution  # noqa: F401
from zerg.models import WorkflowTemplate  # noqa: F401

# Re-export all CRUD functions from the package (single source of truth in __init__.py)
from . import *  # noqa: F401, F403
