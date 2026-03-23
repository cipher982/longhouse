"""Base configuration class for the application.

This module defines the abstract base configuration that all
environment-specific configurations must implement.
"""

from __future__ import annotations

from abc import ABC
from abc import abstractmethod
from dataclasses import dataclass

from zerg.core.interfaces import AuthProvider
from zerg.core.interfaces import Database
from zerg.core.interfaces import EventBus
from zerg.core.interfaces import ModelRegistry


@dataclass
class AppConfig(ABC):
    """Abstract base configuration for the application."""

    @abstractmethod
    def create_database(self) -> Database:
        """Create database instance."""
        pass

    @abstractmethod
    def create_auth_provider(self) -> AuthProvider:
        """Create authentication provider."""
        pass

    @abstractmethod
    def create_model_registry(self) -> ModelRegistry:
        """Create model registry."""
        pass

    @abstractmethod
    def create_event_bus(self) -> EventBus:
        """Create event bus."""
        pass
