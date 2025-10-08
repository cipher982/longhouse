"""Business logic services with dependency injection.

These services contain the core business logic, isolated from infrastructure concerns.
They depend only on abstract interfaces, making them testable and environment-agnostic.
"""

from __future__ import annotations

from typing import Any
from typing import Dict
from typing import List
from typing import Optional

from zerg.core.interfaces import AuthProvider
from zerg.core.interfaces import Database
from zerg.core.interfaces import EventBus
from zerg.core.interfaces import ModelRegistry
from zerg.models.models import Agent
from zerg.models.models import AgentMessage
from zerg.models.models import Thread
from zerg.models.models import User


class AgentService:
    """Service for agent-related business operations."""

    def __init__(
        self,
        database: Database,
        auth_provider: AuthProvider,
        model_registry: ModelRegistry,
        event_bus: EventBus,
    ):
        self.database = database
        self.auth_provider = auth_provider
        self.model_registry = model_registry
        self.event_bus = event_bus

    def list_agents(self, user: User, scope: str = "my") -> List[Agent]:
        """List agents for user."""
        if scope == "my":
            return self.database.get_agents(owner_id=user.id)
        elif scope == "all":
            # Only admins can see all agents
            if getattr(user, "role", "USER") != "ADMIN":
                raise PermissionError("Admin privileges required for scope=all")
            return self.database.get_agents()
        else:
            raise ValueError(f"Invalid scope: {scope}")

    def get_agent(self, agent_id: int, user: User) -> Optional[Agent]:
        """Get single agent by ID."""
        agent = self.database.get_agent(agent_id)
        if not agent:
            return None

        # Check ownership or admin access
        if agent.owner_id != user.id and getattr(user, "role", "USER") != "ADMIN":
            raise PermissionError("Access denied to agent")

        return agent

    async def create_agent(
        self,
        user: User,
        name: str,
        system_instructions: str,
        task_instructions: str,
        model: str,
        schedule: Optional[str] = None,
        config: Optional[Dict[str, Any]] = None,
    ) -> Agent:
        """Create new agent."""
        # Validate model
        if not self.model_registry.is_valid(model):
            raise ValueError(f"Invalid model: {model}")

        # Create agent
        agent = self.database.create_agent(
            owner_id=user.id,
            name=name,
            system_instructions=system_instructions,
            task_instructions=task_instructions,
            model=model,
            schedule=schedule,
            config=config,
        )

        # Store agent ID before publishing event (avoid DetachedInstanceError)
        agent_id = agent.id

        # Publish event
        await self.event_bus.publish("AGENT_CREATED", {"agent_id": agent_id})

        return agent

    async def update_agent(
        self,
        agent_id: int,
        user: User,
        name: Optional[str] = None,
        system_instructions: Optional[str] = None,
        task_instructions: Optional[str] = None,
        model: Optional[str] = None,
        status: Optional[str] = None,
        schedule: Optional[str] = None,
        config: Optional[Dict[str, Any]] = None,
    ) -> Optional[Agent]:
        """Update existing agent."""
        # Check ownership
        existing_agent = self.get_agent(agent_id, user)
        if not existing_agent:
            return None

        # Validate model if provided
        if model and not self.model_registry.is_valid(model):
            raise ValueError(f"Invalid model: {model}")

        # Update agent
        agent = self.database.update_agent(
            agent_id=agent_id,
            name=name,
            system_instructions=system_instructions,
            task_instructions=task_instructions,
            model=model,
            status=status,
            schedule=schedule,
            config=config,
        )

        if agent:
            # Store agent ID before publishing event (avoid DetachedInstanceError)
            agent_id = agent.id
            # Publish event
            await self.event_bus.publish("AGENT_UPDATED", {"agent_id": agent_id})

        return agent

    async def delete_agent(self, agent_id: int, user: User) -> bool:
        """Delete agent."""
        # Check ownership
        existing_agent = self.get_agent(agent_id, user)
        if not existing_agent:
            return False

        # Delete agent
        success = self.database.delete_agent(agent_id)

        if success:
            # Publish event
            await self.event_bus.publish("AGENT_DELETED", {"agent_id": agent_id})

        return success

    def get_agent_messages(self, agent_id: int, user: User, skip: int = 0, limit: int = 100) -> List[AgentMessage]:
        """Get messages for an agent."""
        # Check ownership
        agent = self.get_agent(agent_id, user)
        if not agent:
            raise PermissionError("Access denied to agent")

        return self.database.get_agent_messages(agent_id, skip=skip, limit=limit)

    def create_agent_message(self, agent_id: int, user: User, role: str, content: str) -> AgentMessage:
        """Create message for an agent."""
        # Check ownership
        agent = self.get_agent(agent_id, user)
        if not agent:
            raise PermissionError("Access denied to agent")

        return self.database.create_agent_message(agent_id, role, content)


class ThreadService:
    """Service for thread-related business operations."""

    def __init__(self, database: Database, auth_provider: AuthProvider):
        self.database = database
        self.auth_provider = auth_provider

    def get_threads(self, user: User, agent_id: Optional[int] = None) -> List[Thread]:
        """Get threads for user, optionally filtered by agent."""
        return self.database.get_threads(agent_id=agent_id)

    def create_thread(self, user: User, agent_id: int, title: str) -> Thread:
        """Create new thread."""
        # TODO: Add ownership validation
        return self.database.create_thread(agent_id, title)


class UserService:
    """Service for user-related business operations."""

    def __init__(self, database: Database, auth_provider: AuthProvider):
        self.database = database
        self.auth_provider = auth_provider

    def get_user_by_email(self, email: str) -> Optional[User]:
        """Get user by email."""
        return self.database.get_user_by_email(email)

    def authenticate_user(self, token: str) -> Optional[User]:
        """Authenticate user from token."""
        return self.auth_provider.authenticate(token)
