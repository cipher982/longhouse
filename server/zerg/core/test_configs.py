"""Test-specific configuration classes following industry best practices.

This module provides different configuration classes for various test scenarios:
- UnitTestConfig: Complete isolation with all mocks
- IntegrationTestConfig: Real database with mock external services
- E2ETestConfig: Real implementations in isolated environment

Each configuration has clear separation of concerns and explicit intent.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from zerg.core.base_config import AppConfig
from zerg.core.implementations import ProductionEventBus
from zerg.core.implementations import ProductionModelRegistry
from zerg.core.interfaces import AuthProvider
from zerg.core.interfaces import Database
from zerg.core.interfaces import EventBus
from zerg.core.interfaces import ModelRegistry
from zerg.core.test_implementations import InMemoryEventBus
from zerg.core.test_implementations import IsolatedSQLiteDatabase
from zerg.core.test_implementations import MockModelRegistry
from zerg.core.test_implementations import TestAuthProvider


@dataclass
class BaseTestConfig(AppConfig):
    """Base class for all test configurations with common test functionality."""

    commis_id: str
    db_path: Optional[str] = None

    @classmethod
    def for_commis(cls, commis_id: str) -> BaseTestConfig:
        """Create test configuration for specific commis."""
        return cls(commis_id=commis_id)

    def create_auth_provider(self) -> AuthProvider:
        """All test configs use test authentication."""
        return TestAuthProvider()


@dataclass
class UnitTestConfig(BaseTestConfig):
    """Configuration for unit tests with complete isolation.

    Used for testing individual components in isolation:
    - Mock database (in-memory SQLite)
    - Mock models (gpt-mock, claude-mock)
    - Mock auth (test user)
    - In-memory event bus
    """

    def create_database(self) -> Database:
        """Create isolated in-memory database for unit tests."""
        # For unit tests, keep using IsolatedSQLiteDatabase since they don't use the middleware
        return IsolatedSQLiteDatabase(self.commis_id, ":memory:")

    def create_model_registry(self) -> ModelRegistry:
        """Create mock model registry for unit tests."""
        return MockModelRegistry()

    def create_event_bus(self) -> EventBus:
        """Create in-memory event bus for unit tests."""
        return InMemoryEventBus()


@dataclass
class IntegrationTestConfig(BaseTestConfig):
    """Configuration for integration tests with real database.

    Used for testing database interactions and service integration:
    - Real database (file-based SQLite)
    - Mock models (to avoid external API calls)
    - Mock auth (test user)
    - Real event bus
    """

    def create_database(self) -> Database:
        """Create real file-based database for integration tests."""
        # Use SQLAlchemyDatabase which will use get_session_factory()
        # This picks up the commis context from the middleware dynamically
        from zerg.core.implementations import SQLAlchemyDatabase

        return SQLAlchemyDatabase()

    def create_model_registry(self) -> ModelRegistry:
        """Create mock model registry to avoid external API calls."""
        return MockModelRegistry()

    def create_event_bus(self) -> EventBus:
        """Create real event bus for integration tests."""
        return ProductionEventBus()


@dataclass
class E2ETestConfig(BaseTestConfig):
    """Configuration for end-to-end tests with real implementations.

    Used for testing complete user workflows:
    - Real database (isolated SQLite)
    - Real models (gpt-4o, gpt-4o-mini, etc.)
    - Mock auth (test user for simplicity)
    - Real event bus

    This provides a production-like environment while maintaining test isolation.
    """

    def create_database(self) -> Database:
        """Create isolated database for E2E tests."""
        # Use SQLAlchemyDatabase which will use get_session_factory()
        # This picks up the commis context from the middleware dynamically
        from zerg.core.implementations import SQLAlchemyDatabase

        return SQLAlchemyDatabase()

    def create_model_registry(self) -> ModelRegistry:
        """Create real model registry for E2E tests."""
        return ProductionModelRegistry()

    def create_event_bus(self) -> EventBus:
        """Create real event bus for E2E tests."""
        return ProductionEventBus()


# Factory functions for convenience
def create_unit_test_config(commis_id: str = "0") -> UnitTestConfig:
    """Create unit test configuration."""
    return UnitTestConfig.for_commis(commis_id)


def create_integration_test_config(commis_id: str = "0") -> IntegrationTestConfig:
    """Create integration test configuration."""
    return IntegrationTestConfig.for_commis(commis_id)


def create_e2e_test_config(commis_id: str = "0") -> E2ETestConfig:
    """Create E2E test configuration."""
    return E2ETestConfig.for_commis(commis_id)
