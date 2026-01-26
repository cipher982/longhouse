"""Database models for the application."""

# Re-export from split model files
from .commis_barrier import CommisBarrier
from .commis_barrier import CommisBarrierJob
from .commis_barrier import CommisBarrierJob as BarrierJob  # Backwards compatibility alias
from .connector import Connector
from .fiche import Fiche
from .fiche import Fiche as Agent  # Backwards compatibility alias
from .fiche import FicheMessage
from .fiche import FicheMessage as AgentMessage  # Backwards compatibility alias

# Re-export remaining models from models.py
from .models import AccountConnectorCredential
from .models import AgentMemoryKV
from .models import CanvasLayout
from .models import CommisJob
from .models import ConnectorCredential
from .models import KnowledgeDocument
from .models import KnowledgeSource
from .models import MemoryEmbedding
from .models import MemoryFile
from .models import NodeExecutionState
from .models import Runner
from .models import RunnerEnrollToken
from .models import RunnerJob
from .models import UserSkill
from .models import UserTask
from .models import Workflow
from .models import WorkflowExecution
from .models import WorkflowTemplate
from .run import Run
from .run_event import RunEvent

# Re-export from other modules
from .sync import SyncOperation
from .thread import Thread
from .thread import ThreadMessage
from .trigger import Trigger
from .trigger_config import TriggerConfig
from .user import User
from .waitlist import WaitlistEntry

__all__ = [
    # Core models (split into separate files)
    "Fiche",
    "FicheMessage",
    "Agent",  # Backwards compatibility alias
    "AgentMessage",  # Backwards compatibility alias
    "Run",
    "RunEvent",
    "CommisBarrierJob",
    "BarrierJob",  # Backwards compatibility alias
    "Connector",
    "Thread",
    "ThreadMessage",
    "Trigger",
    "User",
    "WaitlistEntry",
    "CommisBarrier",
    # Remaining models (still in models.py)
    "AccountConnectorCredential",
    "AgentMemoryKV",
    "CanvasLayout",
    "ConnectorCredential",
    "KnowledgeDocument",
    "KnowledgeSource",
    "MemoryEmbedding",
    "MemoryFile",
    "NodeExecutionState",
    "Runner",
    "RunnerEnrollToken",
    "RunnerJob",
    "SyncOperation",
    "TriggerConfig",
    "UserTask",
    "UserSkill",
    "CommisJob",
    "Workflow",
    "WorkflowExecution",
    "WorkflowTemplate",
]
