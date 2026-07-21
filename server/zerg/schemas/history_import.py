"""Typed, privacy-safe history import inventory shared by heartbeat surfaces."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field
from pydantic import model_validator

from zerg.utils.time import UTCBaseModel


class ProviderSourceInventory(UTCBaseModel):
    provider: str = Field(..., min_length=1, max_length=64)
    source_count: int = Field(..., ge=0)
    source_bytes: int = Field(..., ge=0)
    wal_bytes: int = Field(..., ge=0)
    footprint_bytes: int = Field(..., ge=0)
    oldest_modified_at_ms: int | None = Field(None, ge=0)
    newest_modified_at_ms: int | None = Field(None, ge=0)

    @model_validator(mode="after")
    def validate_time_bounds(self) -> ProviderSourceInventory:
        if (
            self.oldest_modified_at_ms is not None
            and self.newest_modified_at_ms is not None
            and self.oldest_modified_at_ms > self.newest_modified_at_ms
        ):
            raise ValueError("oldest_modified_at_ms must not exceed newest_modified_at_ms")
        if self.source_bytes + self.wal_bytes != self.footprint_bytes:
            raise ValueError("source_bytes plus wal_bytes must equal footprint_bytes")
        return self


class SourceInventory(UTCBaseModel):
    schema_version: Literal[1]
    generation: int = Field(..., ge=1)
    content_sha256: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    observed_at: datetime
    scan_duration_ms: int = Field(..., ge=0)
    scan_error_count: int = Field(..., ge=0)
    source_count: int = Field(..., ge=0)
    source_bytes: int = Field(..., ge=0)
    wal_bytes: int = Field(..., ge=0)
    footprint_bytes: int = Field(..., ge=0)
    providers: list[ProviderSourceInventory] = Field(default_factory=list, max_length=64)

    @model_validator(mode="after")
    def validate_aggregates(self) -> SourceInventory:
        provider_names = [item.provider for item in self.providers]
        if provider_names != sorted(set(provider_names)):
            raise ValueError("providers must be unique and sorted")
        if sum(item.source_count for item in self.providers) != self.source_count:
            raise ValueError("provider source counts must equal source_count")
        if sum(item.source_bytes for item in self.providers) != self.source_bytes:
            raise ValueError("provider source bytes must equal source_bytes")
        if sum(item.wal_bytes for item in self.providers) != self.wal_bytes:
            raise ValueError("provider WAL bytes must equal wal_bytes")
        if sum(item.footprint_bytes for item in self.providers) != self.footprint_bytes:
            raise ValueError("provider footprints must equal footprint_bytes")
        if self.source_bytes + self.wal_bytes != self.footprint_bytes:
            raise ValueError("source_bytes plus wal_bytes must equal footprint_bytes")
        return self


class HistoryImportSnapshot(UTCBaseModel):
    state: Literal["discovering", "inventory_ready", "unavailable"]
    inventory: SourceInventory | None = None

    @model_validator(mode="after")
    def validate_state(self) -> HistoryImportSnapshot:
        if self.state == "inventory_ready" and self.inventory is None:
            raise ValueError("inventory_ready requires inventory")
        if self.state != "inventory_ready" and self.inventory is not None:
            raise ValueError("inventory is only valid for inventory_ready")
        return self

    @classmethod
    def unavailable(cls) -> HistoryImportSnapshot:
        return cls(state="unavailable")
