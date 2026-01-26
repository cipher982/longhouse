"""Production implementations of core interfaces.

These implementations wrap the existing CRUD operations and authentication
systems to provide the interface contracts required by business logic.
"""

from __future__ import annotations

from typing import Any
from typing import Dict
from typing import List
from typing import Optional

from fastapi import Request
from sqlalchemy.orm import Session

from zerg.core.interfaces import AuthProvider
from zerg.core.interfaces import Database
from zerg.core.interfaces import EventBus
from zerg.core.interfaces import ModelRegistry
from zerg.crud import crud
from zerg.database import db_session
from zerg.database import get_session_factory
from zerg.models.models import Fiche
from zerg.models.models import FicheMessage
from zerg.models.models import Thread
from zerg.models.models import User


class SQLAlchemyDatabase(Database):
    """Production database implementation using existing SQLAlchemy/CRUD system."""

    def __init__(self, session_factory=None):
        self.session_factory = session_factory or get_session_factory()

    def _get_session(self) -> Session:
        """Get database session."""
        return self.session_factory()

    def get_fiches(self, owner_id: Optional[int] = None, skip: int = 0, limit: int = 100) -> List[Fiche]:
        """Get list of fiches, optionally filtered by owner."""
        with db_session(self.session_factory) as db:
            return crud.get_fiches(db, owner_id=owner_id, skip=skip, limit=limit)

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
            return crud.create_fiche(
                db=db,
                owner_id=owner_id,
                name=name,
                system_instructions=system_instructions,
                task_instructions=task_instructions,
                model=model,
                schedule=schedule,
                config=config,
            )

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


class ProductionAuthProvider(AuthProvider):
    """Production authentication provider using existing JWT system."""

    def authenticate(self, token: str) -> Optional[User]:
        """Authenticate user from token."""
        from zerg.database import get_session_factory
        from zerg.dependencies.auth import _decode_jwt_fallback

        try:
            payload = _decode_jwt_fallback(token)
            email = payload.get("sub")
            if not email:
                return None

            session_factory = get_session_factory()
            with session_factory() as db:
                return crud.get_user_by_email(db, email)
        except Exception:
            return None

    def get_current_user(self, request: Request) -> User:
        """Get current user from request context."""
        from zerg.database import get_session_factory
        from zerg.dependencies.auth import get_current_user

        session_factory = get_session_factory()
        with session_factory() as db:
            return get_current_user(request, db)

    def validate_ws_token(self, token: Optional[str]) -> Optional[User]:
        """Validate WebSocket authentication token."""
        from zerg.database import get_session_factory
        from zerg.dependencies.auth import validate_ws_jwt

        session_factory = get_session_factory()
        with session_factory() as db:
            return validate_ws_jwt(token, db)


class ProductionModelRegistry(ModelRegistry):
    """Production model registry using existing models configuration."""

    def is_valid(self, model_id: str) -> bool:
        """Check if model ID is valid."""
        from zerg.models_config import MODELS_BY_ID

        return model_id in MODELS_BY_ID

    def get_models(self) -> List[Dict[str, Any]]:
        """Get list of available models."""
        from zerg.models_config import MODELS_BY_ID

        return [{"id": model_id, **config} for model_id, config in MODELS_BY_ID.items()]

    def get_model_config(self, model_id: str) -> Optional[Dict[str, Any]]:
        """Get configuration for specific model."""
        from zerg.models_config import MODELS_BY_ID

        return MODELS_BY_ID.get(model_id)


class ProductionEventBus(EventBus):
    """Production event bus using existing event system."""

    async def publish(self, event_type: str, payload: Dict[str, Any]) -> None:
        """Publish event."""
        from zerg.events import EventType
        from zerg.events.event_bus import event_bus

        # Convert string to EventType if it exists
        event_enum = getattr(EventType, event_type.upper(), None)
        if event_enum:
            await event_bus.publish(event_enum, payload)

    async def subscribe(self, event_type: str, handler: Any) -> None:
        """Subscribe to event type."""
        from zerg.events import EventType
        from zerg.events.event_bus import event_bus

        # Convert string to EventType if it exists
        event_enum = getattr(EventType, event_type.upper(), None)
        if event_enum:
            await event_bus.subscribe(event_enum, handler)
