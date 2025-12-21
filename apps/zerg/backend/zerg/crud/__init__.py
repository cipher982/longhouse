"""CRUD operations for all models.

This module provides backwards-compatible imports for all CRUD functions.
"""

# Agent operations
from .crud_agents import create_agent
from .crud_agents import create_agent_message
from .crud_agents import delete_agent
from .crud_agents import get_agent
from .crud_agents import get_agent_messages
from .crud_agents import get_agents
from .crud_agents import update_agent

# Canvas operations
from .crud_canvas import get_canvas_layout
from .crud_canvas import upsert_canvas_layout

# Connector operations
from .crud_connectors import create_connector
from .crud_connectors import delete_connector
from .crud_connectors import get_connector
from .crud_connectors import get_connectors
from .crud_connectors import update_connector

# Message operations
from .crud_messages import create_thread_message
from .crud_messages import get_thread_messages
from .crud_messages import get_unprocessed_messages
from .crud_messages import mark_message_processed
from .crud_messages import mark_messages_processed_bulk

# Run operations
from .crud_runs import create_run
from .crud_runs import list_runs
from .crud_runs import mark_failed
from .crud_runs import mark_finished
from .crud_runs import mark_running

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
from .runner_crud import *  # noqa: F403

__all__ = [
    # Agents
    "create_agent",
    "create_agent_message",
    "delete_agent",
    "get_agent",
    "get_agent_messages",
    "get_agents",
    "update_agent",
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
    # Runs
    "create_run",
    "list_runs",
    "mark_failed",
    "mark_finished",
    "mark_running",
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
