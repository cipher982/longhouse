"""Multi-provider session parsing for the shipper.

Each provider implements three methods:
- discover_files() — find session files on disk
- parse_file() — stream ParsedEvent objects from a session file
- extract_metadata() — extract ParsedSession metadata from a session file

The registry maps provider names to provider instances.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterator
from typing import Protocol
from typing import runtime_checkable

from zerg.services.shipper.parser import ParsedEvent
from zerg.services.shipper.parser import ParsedSession

logger = logging.getLogger(__name__)


@runtime_checkable
class SessionProvider(Protocol):
    """Protocol for session file providers."""

    name: str

    def discover_files(self) -> list[Path]:
        """Find all session files for this provider, newest first."""
        ...

    def parse_file(self, path: Path, offset: int = 0) -> Iterator[ParsedEvent]:
        """Parse events from a session file starting at byte offset."""
        ...

    def extract_metadata(self, path: Path) -> ParsedSession:
        """Extract session metadata from a session file."""
        ...


class ProviderRegistry:
    """Registry of session providers."""

    def __init__(self) -> None:
        self._providers: dict[str, SessionProvider] = {}

    def register(self, provider: SessionProvider) -> None:
        """Register a provider instance."""
        self._providers[provider.name] = provider
        logger.info("Registered session provider: %s", provider.name)

    def get(self, name: str) -> SessionProvider | None:
        """Get a provider by name."""
        return self._providers.get(name)

    def all(self) -> list[SessionProvider]:
        """Get all registered providers."""
        return list(self._providers.values())

    def names(self) -> list[str]:
        """Get all registered provider names."""
        return list(self._providers.keys())


# Global registry instance
registry = ProviderRegistry()
