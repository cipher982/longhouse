"""Test-specific implementations of core interfaces.

These implementations provide isolated, controlled environments for testing
while maintaining the same interfaces as production code.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any
from typing import Dict
from typing import List
from typing import Optional

from fastapi import Request
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from zerg.core.interfaces import AuthProvider
from zerg.core.interfaces import Database
from zerg.core.interfaces import EventBus
from zerg.core.interfaces import ModelRegistry
from zerg.crud import crud
from zerg.database import DB_SCHEMA
from zerg.database import Base
from zerg.database import db_session
from zerg.models.models import Fiche
from zerg.models.models import FicheMessage
from zerg.models.models import Thread
from zerg.models.models import User


class IsolatedSQLiteDatabase(Database):
    """Test database implementation using isolated SQLite files per commis."""

    def __init__(self, commis_id: str, db_path: Optional[str] = None):
        self.commis_id = commis_id

        # Create isolated database file
        if db_path:
            self.db_path = Path(db_path)
        else:
            # Create in temp directory with commis ID
            temp_dir = Path(tempfile.gettempdir()) / "zerg_test_dbs"
            temp_dir.mkdir(exist_ok=True)
            self.db_path = temp_dir / f"test_commis_{commis_id}.db"

        # Create engine and session factory
        base_engine = create_engine(
            f"sqlite:///{self.db_path}",
            connect_args={"check_same_thread": False},
            echo=False,
        )
        self.engine = base_engine.execution_options(schema_translate_map={DB_SCHEMA: None})
        self.session_factory = sessionmaker(
            autocommit=False,
            autoflush=False,
            expire_on_commit=False,  # Keep objects accessible after commit
            bind=self.engine,
        )

        # Create a scoped session for better session management
        from sqlalchemy.orm import scoped_session

        self.scoped_session = scoped_session(self.session_factory)

        # Initialize database schema
        self._initialize_schema()

    def _initialize_schema(self):
        """Initialize database schema and create test user."""
        # Create all tables
        Base.metadata.create_all(bind=self.engine)

        # Create test user for foreign key constraints
        with db_session(self.session_factory) as db:
            existing_user = crud.get_user_by_email(db, "test@example.com")
            if not existing_user:
                crud.create_user(db, email="test@example.com", role="ADMIN", provider="dev", provider_user_id="test-user-1")

    def cleanup(self):
        """Clean up database file."""
        if self.db_path.exists():
            self.db_path.unlink()

    def get_fiches(self, owner_id: Optional[int] = None, skip: int = 0, limit: int = 100) -> List[Fiche]:
        """Get list of fiches, optionally filtered by owner."""
        with db_session(self.session_factory) as db:
            fiches = crud.get_fiches(db, owner_id=owner_id, skip=skip, limit=limit)
            # Force load relationships before session closes
            for fiche in fiches:
                _ = fiche.owner
                _ = fiche.messages
            return fiches

    def get_fiche(self, fiche_id: int) -> Optional[Fiche]:
        """Get single fiche by ID."""
        with db_session(self.session_factory) as db:
            return crud.get_fiche(db, fiche_id)

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
        with db_session(self.session_factory) as db:
            fiche = crud.create_fiche(
                db=db,
                owner_id=owner_id,
                name=name,
                system_instructions=system_instructions,
                task_instructions=task_instructions,
                model=model,
                schedule=schedule,
                config=config,
            )
            # Force load relationships before session closes
            _ = fiche.owner
            _ = fiche.messages
            return fiche

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
        with db_session(self.session_factory) as db:
            return crud.update_fiche(
                db=db,
                fiche_id=fiche_id,
                name=name,
                system_instructions=system_instructions,
                task_instructions=task_instructions,
                model=model,
                status=status,
                schedule=schedule,
                config=config,
            )

    def delete_fiche(self, fiche_id: int) -> bool:
        """Delete fiche by ID."""
        with db_session(self.session_factory) as db:
            return crud.delete_fiche(db, fiche_id)

    def get_user_by_email(self, email: str) -> Optional[User]:
        """Get user by email address."""
        with db_session(self.session_factory) as db:
            return crud.get_user_by_email(db, email)

    def create_user(self, email: str, **kwargs) -> User:
        """Create new user."""
        with db_session(self.session_factory) as db:
            return crud.create_user(db, email=email, **kwargs)

    def get_threads(self, fiche_id: Optional[int] = None, owner_id: Optional[int] = None) -> List[Thread]:
        """Get threads, optionally filtered by fiche or owner."""
        with db_session(self.session_factory) as db:
            return crud.get_threads(db, fiche_id=fiche_id, owner_id=owner_id)

    def create_thread(self, fiche_id: int, title: str) -> Thread:
        """Create new thread."""
        with db_session(self.session_factory) as db:
            return crud.create_thread(db, fiche_id=fiche_id, title=title)

    def get_fiche_messages(self, fiche_id: int, skip: int = 0, limit: int = 100) -> List[FicheMessage]:
        """Get messages for a fiche."""
        with db_session(self.session_factory) as db:
            return crud.get_fiche_messages(db, fiche_id=fiche_id, skip=skip, limit=limit)

    def create_fiche_message(self, fiche_id: int, role: str, content: str) -> FicheMessage:
        """Create new fiche message."""
        with db_session(self.session_factory) as db:
            return crud.create_fiche_message(db, fiche_id=fiche_id, role=role, content=content)


class TestAuthProvider(AuthProvider):
    """Test authentication provider that bypasses real authentication."""

    def __init__(self, test_user: Optional[User] = None):
        self.test_user = test_user or self._create_default_test_user()

    def _create_default_test_user(self) -> User:
        """Create default test user object."""
        # Create a mock user object for testing
        from zerg.models.models import User as UserModel

        user = UserModel()
        user.id = 1
        user.email = "test@example.com"
        user.role = "ADMIN"
        user.is_active = True
        user.provider = "dev"
        user.provider_user_id = "test-user-1"
        user.display_name = "Test User"
        return user

    def authenticate(self, token: str) -> Optional[User]:
        """Authenticate user from token (always returns test user)."""
        return self.test_user

    def get_current_user(self, request: Request) -> User:
        """Get current user from request context (always returns test user)."""
        return self.test_user

    def validate_ws_token(self, token: Optional[str]) -> Optional[User]:
        """Validate WebSocket authentication token (always returns test user)."""
        return self.test_user


class MockModelRegistry(ModelRegistry):
    """Mock model registry for testing."""

    def __init__(self):
        self.models = {
            "gpt-mock": {
                "name": "GPT Mock",
                "description": "Mock model for testing",
                "provider": "mock",
                "context_window": 4096,
            },
            "claude-mock": {
                "name": "Claude Mock",
                "description": "Mock Claude model for testing",
                "provider": "mock",
                "context_window": 8192,
            },
        }

    def is_valid(self, model_id: str) -> bool:
        """Check if model ID is valid."""
        return model_id in self.models

    def get_models(self) -> List[Dict[str, Any]]:
        """Get list of available models."""
        return [{"id": model_id, **config} for model_id, config in self.models.items()]

    def get_model_config(self, model_id: str) -> Optional[Dict[str, Any]]:
        """Get configuration for specific model."""
        return self.models.get(model_id)


class InMemoryEventBus(EventBus):
    """In-memory event bus for testing."""

    def __init__(self):
        self.events: List[Dict[str, Any]] = []
        self.subscribers: Dict[str, List[Any]] = {}

    async def publish(self, event_type: str, payload: Dict[str, Any]) -> None:
        """Publish event."""
        event = {
            "type": event_type,
            "payload": payload,
            "timestamp": "2024-01-01T00:00:00Z",  # Fixed timestamp for testing
        }
        self.events.append(event)

        # Notify subscribers
        if event_type in self.subscribers:
            for handler in self.subscribers[event_type]:
                await handler(event)

    async def subscribe(self, event_type: str, handler: Any) -> None:
        """Subscribe to event type."""
        if event_type not in self.subscribers:
            self.subscribers[event_type] = []
        self.subscribers[event_type].append(handler)

    def get_events(self) -> List[Dict[str, Any]]:
        """Get all published events (test helper)."""
        return self.events.copy()

    def clear_events(self) -> None:
        """Clear all events (test helper)."""
        self.events.clear()
