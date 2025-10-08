"""Configuration classes for different application environments.

These classes define the dependencies and settings for production, testing,
and development environments while maintaining clean separation.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from zerg.core.base_config import AppConfig
from zerg.core.implementations import ProductionAuthProvider
from zerg.core.implementations import ProductionEventBus
from zerg.core.implementations import ProductionModelRegistry
from zerg.core.implementations import SQLAlchemyDatabase
from zerg.core.interfaces import AuthProvider
from zerg.core.interfaces import Database
from zerg.core.interfaces import EventBus
from zerg.core.interfaces import ModelRegistry
from zerg.core.test_configs import E2ETestConfig
from zerg.core.test_configs import IntegrationTestConfig
from zerg.core.test_configs import UnitTestConfig
from zerg.core.test_implementations import InMemoryEventBus
from zerg.core.test_implementations import MockModelRegistry
from zerg.core.test_implementations import TestAuthProvider


@dataclass
class ProductionConfig(AppConfig):
    """Production configuration using real infrastructure."""

    database_url: str
    jwt_secret: str
    openai_api_key: str
    cors_origins: list[str]

    @classmethod
    def from_env(cls) -> ProductionConfig:
        """Create configuration from environment variables."""
        return cls(
            database_url=os.getenv("DATABASE_URL", "sqlite:///./app.db"),
            jwt_secret=os.getenv("JWT_SECRET", "dev-secret"),
            openai_api_key=os.getenv("OPENAI_API_KEY", ""),
            cors_origins=os.getenv("ALLOWED_CORS_ORIGINS", "").split(","),
        )

    def create_database(self) -> Database:
        """Create production database."""
        return SQLAlchemyDatabase()

    def create_auth_provider(self) -> AuthProvider:
        """Create production authentication provider."""
        return ProductionAuthProvider()

    def create_model_registry(self) -> ModelRegistry:
        """Create production model registry."""
        return ProductionModelRegistry()

    def create_event_bus(self) -> EventBus:
        """Create production event bus."""
        return ProductionEventBus()


@dataclass
class TestConfig(AppConfig):
    """Legacy test configuration - DEPRECATED.

    This class is kept for backward compatibility but should not be used
    for new tests. Use the specific test configurations instead:
    - UnitTestConfig for unit tests
    - IntegrationTestConfig for integration tests
    - E2ETestConfig for end-to-end tests
    """

    worker_id: str
    db_path: Optional[str] = None
    mock_responses: Optional[dict] = None

    @classmethod
    def for_worker(cls, worker_id: str) -> TestConfig:
        """Create test configuration for specific worker."""
        # For backward compatibility, use UnitTestConfig
        return UnitTestConfig.for_worker(worker_id)

    def create_database(self) -> Database:
        """Create isolated test database."""
        # Use SQLAlchemyDatabase which will use get_session_factory()
        # This picks up the worker context from the middleware dynamically
        from zerg.core.implementations import SQLAlchemyDatabase

        return SQLAlchemyDatabase()

    def create_auth_provider(self) -> AuthProvider:
        """Create test authentication provider."""
        return TestAuthProvider()

    def create_model_registry(self) -> ModelRegistry:
        """Create mock model registry."""
        return MockModelRegistry()

    def create_event_bus(self) -> EventBus:
        """Create in-memory event bus."""
        return InMemoryEventBus()


@dataclass
class DevelopmentConfig(AppConfig):
    """Development configuration mixing real and mock services."""

    database_url: str = "sqlite:///./dev.db"
    use_mock_auth: bool = True
    use_mock_models: bool = True

    def create_database(self) -> Database:
        """Create development database."""
        return SQLAlchemyDatabase()

    def create_auth_provider(self) -> AuthProvider:
        """Create development authentication provider."""
        if self.use_mock_auth:
            return TestAuthProvider()
        return ProductionAuthProvider()

    def create_model_registry(self) -> ModelRegistry:
        """Create development model registry."""
        if self.use_mock_models:
            return MockModelRegistry()
        return ProductionModelRegistry()

    def create_event_bus(self) -> EventBus:
        """Create development event bus."""
        return InMemoryEventBus()  # Faster for development


def load_config() -> AppConfig:
    """Load configuration based on environment.

    Environment variables:
    - ENVIRONMENT: production, development, test, test:unit, test:integration, test:e2e
    - TEST_WORKER_ID: Worker ID for parallel test execution
    - TEST_TYPE: Override for test type (unit, integration, e2e)
    """
    environment = os.getenv("ENVIRONMENT", "production")

    # Handle test environments with optional subtype
    if environment.startswith("test"):
        worker_id = os.getenv("TEST_WORKER_ID", "0")

        # Check for explicit test type
        if ":" in environment:
            test_type = environment.split(":")[1]
        else:
            # Fall back to TEST_TYPE env var or default to unit
            test_type = os.getenv("TEST_TYPE", "unit")

        if test_type == "e2e":
            return E2ETestConfig.for_worker(worker_id)
        elif test_type == "integration":
            return IntegrationTestConfig.for_worker(worker_id)
        else:
            # Default to unit test config for backward compatibility
            return UnitTestConfig.for_worker(worker_id)
    elif environment == "development":
        return DevelopmentConfig()
    else:
        return ProductionConfig.from_env()
