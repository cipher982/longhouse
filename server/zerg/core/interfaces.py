"""Abstract interfaces for core business operations.

These interfaces define the contracts that business logic depends on,
allowing different implementations for production, testing, and development.
"""

from __future__ import annotations

from abc import ABC
from abc import abstractmethod
from typing import Any
from typing import Dict
from typing import List
from typing import Optional

from zerg.models.models import Fiche
from zerg.models.models import FicheMessage
from zerg.models.models import Thread
from zerg.models.models import User


class Database(ABC):
    """Abstract interface for database operations."""

    # Fiche operations
    @abstractmethod
    def get_fiches(self, owner_id: Optional[int] = None, skip: int = 0, limit: int = 100) -> List[Fiche]:
        """Get list of fiches, optionally filtered by owner."""
        pass

    @abstractmethod
    def get_fiche(self, fiche_id: int) -> Optional[Fiche]:
        """Get single fiche by ID."""
        pass

    @abstractmethod
    def create_fiche(
        self,
        owner_id: int,
        name: str,
        system_instructions: str,
        task_instructions: str,
        model: str,
        schedule: Optional[str] = None,
        config: Optional[Dict[str, Any]] = None,
    ) -> Fiche:
        """Create new fiche."""
        pass

    @abstractmethod
    def update_fiche(
        self,
        fiche_id: int,
        name: Optional[str] = None,
        system_instructions: Optional[str] = None,
        task_instructions: Optional[str] = None,
        model: Optional[str] = None,
        status: Optional[str] = None,
        schedule: Optional[str] = None,
        config: Optional[Dict[str, Any]] = None,
    ) -> Optional[Fiche]:
        """Update existing fiche."""
        pass

    @abstractmethod
    def delete_fiche(self, fiche_id: int) -> bool:
        """Delete fiche by ID."""
        pass

    # User operations
    @abstractmethod
    def get_user_by_email(self, email: str) -> Optional[User]:
        """Get user by email address."""
        pass

    @abstractmethod
    def create_user(self, email: str, **kwargs) -> User:
        """Create new user."""
        pass

    # Thread operations
    @abstractmethod
    def get_threads(self, fiche_id: Optional[int] = None, owner_id: Optional[int] = None) -> List[Thread]:
        """Get threads, optionally filtered by fiche or owner."""
        pass

    @abstractmethod
    def create_thread(self, fiche_id: int, title: str) -> Thread:
        """Create new thread."""
        pass

    # Message operations
    @abstractmethod
    def get_fiche_messages(self, fiche_id: int, skip: int = 0, limit: int = 100) -> List[FicheMessage]:
        """Get messages for a fiche."""
        pass

    @abstractmethod
    def create_fiche_message(self, fiche_id: int, role: str, content: str) -> FicheMessage:
        """Create new fiche message."""
        pass


class AuthProvider(ABC):
    """Abstract interface for authentication operations."""

    @abstractmethod
    def authenticate(self, token: str) -> Optional[User]:
        """Authenticate user from token."""
        pass

    @abstractmethod
    def get_current_user(self, request: Any) -> User:
        """Get current user from request context."""
        pass

    @abstractmethod
    def validate_ws_token(self, token: Optional[str]) -> Optional[User]:
        """Validate WebSocket authentication token."""
        pass


class ModelRegistry(ABC):
    """Abstract interface for AI model management."""

    @abstractmethod
    def is_valid(self, model_id: str) -> bool:
        """Check if model ID is valid."""
        pass

    @abstractmethod
    def get_models(self) -> List[Dict[str, Any]]:
        """Get list of available models."""
        pass

    @abstractmethod
    def get_model_config(self, model_id: str) -> Optional[Dict[str, Any]]:
        """Get configuration for specific model."""
        pass


class EventBus(ABC):
    """Abstract interface for event publishing."""

    @abstractmethod
    async def publish(self, event_type: str, payload: Dict[str, Any]) -> None:
        """Publish event."""
        pass

    @abstractmethod
    async def subscribe(self, event_type: str, handler: Any) -> None:
        """Subscribe to event type."""
        pass
