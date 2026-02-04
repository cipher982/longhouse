"""CRUD operations for all models."""

# Fiche operations
# Connector operations
from .crud_connectors import create_connector
from .crud_connectors import delete_connector
from .crud_connectors import get_connector
from .crud_connectors import get_connectors
from .crud_connectors import update_connector
from .crud_fiches import create_fiche
from .crud_fiches import create_fiche_message
from .crud_fiches import delete_fiche
from .crud_fiches import get_fiche
from .crud_fiches import get_fiche_messages
from .crud_fiches import get_fiches
from .crud_fiches import update_fiche

# Message operations
from .crud_messages import create_thread_message
from .crud_messages import get_thread_messages
from .crud_messages import get_unprocessed_messages
from .crud_messages import mark_message_processed
from .crud_messages import mark_messages_processed_bulk

# Run operations
from .crud_runs import create_run
from .crud_runs import list_runs
from .crud_runs import mark_run_failed
from .crud_runs import mark_run_finished
from .crud_runs import mark_run_running

# User skill operations
from .crud_skills import create_user_skill
from .crud_skills import delete_user_skill
from .crud_skills import get_user_skill_by_name
from .crud_skills import list_user_skills
from .crud_skills import update_user_skill

# Thread operations
from .crud_threads import create_thread
from .crud_threads import delete_thread
from .crud_threads import get_active_thread
from .crud_threads import get_thread
from .crud_threads import get_threads
from .crud_threads import update_thread

# Trigger operations
from .crud_triggers import create_trigger
from .crud_triggers import delete_trigger
from .crud_triggers import get_trigger
from .crud_triggers import get_triggers

# User operations
from .crud_users import count_users
from .crud_users import create_user
from .crud_users import get_user
from .crud_users import get_user_by_email
from .crud_users import update_user

# Re-export from specialized modules
from .knowledge_crud import *  # noqa: F403
from .memory_crud import *  # noqa: F403
from .runner_crud import *  # noqa: F403

__all__ = [
    # Fiches
    "create_fiche",
    "create_fiche_message",
    "delete_fiche",
    "get_fiche",
    "get_fiche_messages",
    "get_fiches",
    "update_fiche",
    # Connectors
    "create_connector",
    "delete_connector",
    "get_connector",
    "get_connectors",
    "update_connector",
    # Messages
    "create_thread_message",
    "get_thread_messages",
    "get_unprocessed_messages",
    "mark_message_processed",
    "mark_messages_processed_bulk",
    # Runs
    "create_run",
    "list_runs",
    "mark_run_failed",
    "mark_run_finished",
    "mark_run_running",
    # Threads
    "create_thread",
    "delete_thread",
    "get_active_thread",
    "get_thread",
    "get_threads",
    "update_thread",
    # Triggers
    "create_trigger",
    "delete_trigger",
    "get_trigger",
    "get_triggers",
    # Users
    "count_users",
    "create_user",
    "get_user",
    "get_user_by_email",
    "update_user",
    # User Skills
    "create_user_skill",
    "delete_user_skill",
    "get_user_skill_by_name",
    "list_user_skills",
    "update_user_skill",
]
