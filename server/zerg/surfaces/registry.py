"""Registry for Oikos surface adapters."""

from __future__ import annotations

from dataclasses import dataclass

from zerg.surfaces.base import SurfaceAdapter


class SurfaceRegistryError(Exception):
    """Raised when adapter registration or lookup fails."""


@dataclass
class SurfaceRegistry:
    """In-memory registry mapping surface IDs to adapters."""

    def __init__(self) -> None:
        self._adapters: dict[str, SurfaceAdapter] = {}

    def register(self, adapter: SurfaceAdapter, *, replace: bool = False) -> None:
        surface_id = adapter.surface_id.strip().lower()
        if not surface_id:
            raise SurfaceRegistryError("adapter surface_id is required")
        if surface_id in self._adapters and not replace:
            raise SurfaceRegistryError(f"surface adapter '{surface_id}' already registered")
        self._adapters[surface_id] = adapter

    def get(self, surface_id: str) -> SurfaceAdapter | None:
        return self._adapters.get(surface_id.strip().lower())

    def require(self, surface_id: str) -> SurfaceAdapter:
        adapter = self.get(surface_id)
        if adapter is None:
            raise SurfaceRegistryError(f"unknown surface adapter: {surface_id}")
        return adapter

    def list_ids(self) -> list[str]:
        return sorted(self._adapters.keys())
