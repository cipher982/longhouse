"""CRUD operations for all models."""

# Fiche operations
# Canvas operations
from .crud_canvas import get_canvas_layout
from .crud_canvas import upsert_canvas_layout

# Connector operations
from .crud_connectors import create_connector
from .crud_connectors import delete_connector
from .crud_connectors import get_connector
from .crud_connectors import get_connectors
from .crud_connectors import update_connector

# Course operations
from .crud_courses import create_course
from .crud_courses import list_courses
from .crud_courses import mark_course_failed
from .crud_courses import mark_course_finished
from .crud_courses import mark_course_running
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

# Workflow operations
from .crud_workflows import create_workflow
from .crud_workflows import create_workflow_execution
from .crud_workflows import create_workflow_template
from .crud_workflows import deploy_workflow_template
from .crud_workflows import get_template_categories
from .crud_workflows import get_waiting_execution_for_workflow
from .crud_workflows import get_workflow
from .crud_workflows import get_workflow_execution
from .crud_workflows import get_workflow_executions
from .crud_workflows import get_workflow_template
from .crud_workflows import get_workflow_template_by_name
from .crud_workflows import get_workflow_templates
from .crud_workflows import get_workflows

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
    # Canvas
    "get_canvas_layout",
    "upsert_canvas_layout",
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
    # Courses
    "create_course",
    "list_courses",
    "mark_course_failed",
    "mark_course_finished",
    "mark_course_running",
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
    # Workflows
    "create_workflow",
    "create_workflow_execution",
    "create_workflow_template",
    "deploy_workflow_template",
    "get_template_categories",
    "get_waiting_execution_for_workflow",
    "get_workflow",
    "get_workflow_execution",
    "get_workflow_executions",
    "get_workflow_template",
    "get_workflow_template_by_name",
    "get_workflow_templates",
    "get_workflows",
]
