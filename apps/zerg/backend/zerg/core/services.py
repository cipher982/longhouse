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
from zerg.models.models import Fiche
from zerg.models.models import FicheMessage
from zerg.models.models import Thread
from zerg.models.models import User


class FicheService:
    """Service for fiche-related business operations."""

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

    def list_fiches(self, user: User, scope: str = "my") -> List[Fiche]:
        """List fiches for user."""
        if scope == "my":
            return self.database.get_fiches(owner_id=user.id)
        elif scope == "all":
            # Only admins can see all fiches
            if getattr(user, "role", "USER") != "ADMIN":
                raise PermissionError("Admin privileges required for scope=all")
            return self.database.get_fiches()
        else:
            raise ValueError(f"Invalid scope: {scope}")

    def get_fiche(self, fiche_id: int, user: User) -> Optional[Fiche]:
        """Get single fiche by ID."""
        fiche = self.database.get_fiche(fiche_id)
        if not fiche:
            return None

        # Check ownership or admin access
        if fiche.owner_id != user.id and getattr(user, "role", "USER") != "ADMIN":
            raise PermissionError("Access denied to fiche")

        return fiche

    async def create_fiche(
        self,
        user: User,
        name: str,
        system_instructions: str,
        task_instructions: str,
        model: str,
        schedule: Optional[str] = None,
        config: Optional[Dict[str, Any]] = None,
    ) -> Fiche:
        """Create new fiche."""
        # Validate model
        if not self.model_registry.is_valid(model):
            raise ValueError(f"Invalid model: {model}")

        # Create fiche
        fiche = self.database.create_fiche(
            owner_id=user.id,
            name=name,
            system_instructions=system_instructions,
            task_instructions=task_instructions,
            model=model,
            schedule=schedule,
            config=config,
        )

        # Store fiche ID before publishing event (avoid DetachedInstanceError)
        fiche_id = fiche.id

        # Publish event
        await self.event_bus.publish("FICHE_CREATED", {"fiche_id": fiche_id})

        return fiche

    async def update_fiche(
        self,
        fiche_id: int,
        user: User,
        name: Optional[str] = None,
        system_instructions: Optional[str] = None,
        task_instructions: Optional[str] = None,
        model: Optional[str] = None,
        status: Optional[str] = None,
        schedule: Optional[str] = None,
        config: Optional[Dict[str, Any]] = None,
    ) -> Optional[Fiche]:
        """Update existing fiche."""
        # Check ownership
        existing_fiche = self.get_fiche(fiche_id, user)
        if not existing_fiche:
            return None

        # Validate model if provided
        if model and not self.model_registry.is_valid(model):
            raise ValueError(f"Invalid model: {model}")

        # Update fiche
        fiche = self.database.update_fiche(
            fiche_id=fiche_id,
            name=name,
            system_instructions=system_instructions,
            task_instructions=task_instructions,
            model=model,
            status=status,
            schedule=schedule,
            config=config,
        )

        if fiche:
            # Store fiche ID before publishing event (avoid DetachedInstanceError)
            fiche_id = fiche.id
            # Publish event
            await self.event_bus.publish("FICHE_UPDATED", {"fiche_id": fiche_id})

        return fiche

    async def delete_fiche(self, fiche_id: int, user: User) -> bool:
        """Delete fiche."""
        # Check ownership
        existing_fiche = self.get_fiche(fiche_id, user)
        if not existing_fiche:
            return False

        # Delete fiche
        success = self.database.delete_fiche(fiche_id)

        if success:
            # Publish event
            await self.event_bus.publish("FICHE_DELETED", {"fiche_id": fiche_id})

        return success

    def get_fiche_messages(self, fiche_id: int, user: User, skip: int = 0, limit: int = 100) -> List[FicheMessage]:
        """Get messages for a fiche."""
        # Check ownership
        fiche = self.get_fiche(fiche_id, user)
        if not fiche:
            raise PermissionError("Access denied to fiche")

        return self.database.get_fiche_messages(fiche_id, skip=skip, limit=limit)

    def create_fiche_message(self, fiche_id: int, user: User, role: str, content: str) -> FicheMessage:
        """Create message for a fiche."""
        # Check ownership
        fiche = self.get_fiche(fiche_id, user)
        if not fiche:
            raise PermissionError("Access denied to fiche")

        return self.database.create_fiche_message(fiche_id, role, content)


class ThreadService:
    """Service for thread-related business operations."""

    def __init__(self, database: Database, auth_provider: AuthProvider):
        self.database = database
        self.auth_provider = auth_provider

    def get_threads(self, user: User, fiche_id: Optional[int] = None) -> List[Thread]:
        """Get threads for user, optionally filtered by fiche."""
        is_admin = getattr(user, "role", "USER") == "ADMIN"

        if fiche_id is not None:
            fiche = self.database.get_fiche(fiche_id)
            if fiche is None:
                return []
            if not is_admin and fiche.owner_id != user.id:
                raise PermissionError("Access denied to fiche")
            return self.database.get_threads(fiche_id=fiche_id)

        if is_admin:
            return self.database.get_threads()

        # Single query with owner_id join (avoids O(n) fiche iteration)
        return self.database.get_threads(owner_id=user.id)

    def create_thread(self, user: User, fiche_id: int, title: str) -> Thread:
        """Create new thread."""
        is_admin = getattr(user, "role", "USER") == "ADMIN"
        fiche = self.database.get_fiche(fiche_id)
        if fiche is None:
            raise ValueError("Fiche not found")
        if not is_admin and fiche.owner_id != user.id:
            raise PermissionError("Access denied to fiche")
        return self.database.create_thread(fiche_id, title)


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
