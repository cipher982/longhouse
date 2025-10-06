"""Application factory for creating FastAPI applications with dependency injection.

This module provides the main application factory that wires together
business services with infrastructure implementations based on configuration.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import Depends
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from zerg.core.config import AppConfig
from zerg.core.config import load_config
from zerg.core.interfaces import AuthProvider
from zerg.core.interfaces import Database
from zerg.core.interfaces import EventBus
from zerg.core.interfaces import ModelRegistry
from zerg.core.services import AgentService
from zerg.core.services import ThreadService
from zerg.core.services import UserService

# Global configuration instance
_app_config: AppConfig | None = None


def get_app_config() -> AppConfig:
    """Get the global application configuration."""
    global _app_config
    if _app_config is None:
        _app_config = load_config()
    return _app_config


def get_database() -> Database:
    """Dependency provider for Database."""
    return get_app_config().create_database()


def get_auth_provider() -> AuthProvider:
    """Dependency provider for AuthProvider."""
    return get_app_config().create_auth_provider()


def get_model_registry() -> ModelRegistry:
    """Dependency provider for ModelRegistry."""
    return get_app_config().create_model_registry()


def get_event_bus() -> EventBus:
    """Dependency provider for EventBus."""
    return get_app_config().create_event_bus()


def get_agent_service(
    database: Database = Depends(get_database),
    auth_provider: AuthProvider = Depends(get_auth_provider),
    model_registry: ModelRegistry = Depends(get_model_registry),
    event_bus: EventBus = Depends(get_event_bus),
) -> AgentService:
    """Dependency provider for AgentService."""
    return AgentService(database, auth_provider, model_registry, event_bus)


def get_thread_service(
    database: Database = Depends(get_database),
    auth_provider: AuthProvider = Depends(get_auth_provider),
) -> ThreadService:
    """Dependency provider for ThreadService."""
    return ThreadService(database, auth_provider)


def get_user_service(
    database: Database = Depends(get_database),
    auth_provider: AuthProvider = Depends(get_auth_provider),
) -> UserService:
    """Dependency provider for UserService."""
    return UserService(database, auth_provider)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Manage application lifespan."""
    # Startup
    print(f"Starting application with config: {type(get_app_config()).__name__}")

    # Cleanup any existing test databases if in test mode
    config = get_app_config()
    if hasattr(config, "worker_id"):
        # Test mode - ensure clean database
        database = config.create_database()
        if hasattr(database, "cleanup"):
            database.cleanup()

    yield

    # Shutdown
    print("Shutting down application")


def create_app(config: AppConfig | None = None) -> FastAPI:
    """Create FastAPI application with dependency injection."""
    global _app_config

    # Override global config if provided
    if config:
        _app_config = config

    # Create FastAPI app
    app = FastAPI(
        title="Agent Platform API",
        description="AI Agent Platform with clean architecture",
        version="2.0.0",
        lifespan=lifespan,
    )

    # CORS middleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # Configure based on environment
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Import and include routers
    from zerg.core.routers import agent_router
    from zerg.core.routers import thread_router
    from zerg.core.routers import user_router

    app.include_router(agent_router, prefix="/api/agents", tags=["agents"])
    app.include_router(thread_router, prefix="/api/threads", tags=["threads"])
    app.include_router(user_router, prefix="/api/users", tags=["users"])

    # Legacy admin endpoint for database reset
    from zerg.routers.admin import router as admin_router

    app.include_router(admin_router, prefix="/api")

    # Health check endpoint
    @app.get("/")
    async def health_check():
        return {"status": "healthy", "config": type(get_app_config()).__name__}

    return app


def create_production_app() -> FastAPI:
    """Create production application."""
    from zerg.core.config import ProductionConfig

    return create_app(ProductionConfig.from_env())


def create_test_app(worker_id: str) -> FastAPI:
    """Create test application for specific worker."""
    from zerg.core.config import TestConfig

    return create_app(TestConfig.for_worker(worker_id))


def create_development_app() -> FastAPI:
    """Create development application."""
    from zerg.core.config import DevelopmentConfig

    return create_app(DevelopmentConfig())
