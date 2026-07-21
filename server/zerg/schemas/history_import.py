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


class ProviderHistoryProgress(UTCBaseModel):
    provider: str = Field(..., min_length=1, max_length=64)
    unit: Literal["bytes", "records", "unknown"]
    inventory_source_count: int = Field(..., ge=0)
    inventory_source_bytes: int = Field(..., ge=0)
    tracked_source_count: int = Field(..., ge=0)
    complete_source_count: int = Field(..., ge=0)
    observed_units: int = Field(..., ge=0)
    acknowledged_units: int = Field(..., ge=0)
    remaining_units: int = Field(..., ge=0)
    exact_total: bool
    inventory_coverage_complete: bool

    @model_validator(mode="after")
    def validate_progress(self) -> ProviderHistoryProgress:
        if self.complete_source_count > self.tracked_source_count:
            raise ValueError("complete_source_count must not exceed tracked_source_count")
        if self.acknowledged_units > self.observed_units:
            raise ValueError("acknowledged_units must not exceed observed_units")
        if self.acknowledged_units + self.remaining_units != self.observed_units:
            raise ValueError("acknowledged_units plus remaining_units must equal observed_units")
        if self.exact_total and self.unit != "bytes":
            raise ValueError("only byte progress can have an exact inventory denominator")
        if self.exact_total and self.observed_units != self.inventory_source_bytes:
            raise ValueError("exact byte progress must use inventory_source_bytes")
        if self.unit == "bytes" and self.observed_units < self.inventory_source_bytes:
            raise ValueError("byte progress cannot observe fewer bytes than inventory")
        if self.unit == "unknown" and self.inventory_coverage_complete:
            raise ValueError("unknown progress units cannot claim complete inventory coverage")
        return self


class HistoryImportProgress(UTCBaseModel):
    acknowledged_source_bytes: int = Field(..., ge=0)
    remaining_source_bytes: int = Field(..., ge=0)
    acknowledged_records: int = Field(..., ge=0)
    remaining_records: int = Field(..., ge=0)
    pending_outbox_count: int = Field(..., ge=0)
    pending_outbox_bytes: int = Field(..., ge=0)
    blocked_source_count: int = Field(..., ge=0)
    blocked_bytes: int = Field(..., ge=0)
    latest_block_kind: str | None = Field(None, max_length=128)
    providers: list[ProviderHistoryProgress] = Field(default_factory=list, max_length=64)

    @model_validator(mode="after")
    def validate_aggregates(self) -> HistoryImportProgress:
        provider_names = [item.provider for item in self.providers]
        if provider_names != sorted(set(provider_names)):
            raise ValueError("progress providers must be unique and sorted")
        byte_providers = [item for item in self.providers if item.unit == "bytes"]
        record_providers = [item for item in self.providers if item.unit == "records"]
        if sum(item.acknowledged_units for item in byte_providers) != self.acknowledged_source_bytes:
            raise ValueError("byte provider acknowledgements must equal acknowledged_source_bytes")
        if sum(item.remaining_units for item in byte_providers) != self.remaining_source_bytes:
            raise ValueError("byte provider remaining units must equal remaining_source_bytes")
        if sum(item.acknowledged_units for item in record_providers) != self.acknowledged_records:
            raise ValueError("record provider acknowledgements must equal acknowledged_records")
        if sum(item.remaining_units for item in record_providers) != self.remaining_records:
            raise ValueError("record provider remaining units must equal remaining_records")
        return self


class HistoryImportSnapshot(UTCBaseModel):
    state: Literal[
        "discovering",
        "inventory_ready",
        "importing",
        "paused",
        "backpressured",
        "blocked_source",
        "offline",
        "current",
        "unavailable",
    ]
    inventory: SourceInventory | None = None
    progress: HistoryImportProgress | None = None

    @model_validator(mode="after")
    def validate_state(self) -> HistoryImportSnapshot:
        without_inventory = self.state in {"discovering", "unavailable"}
        if without_inventory and (self.inventory is not None or self.progress is not None):
            raise ValueError("discovery and unavailable states cannot include inventory progress")
        if not without_inventory and self.inventory is None:
            raise ValueError(f"{self.state} requires inventory")
        if self.progress is not None and self.inventory is None:
            raise ValueError("progress requires inventory")
        if self.state == "current":
            if self.progress is None:
                raise ValueError("current requires durable progress")
            if self.inventory is None or self.inventory.scan_error_count > 0:
                raise ValueError("current requires an error-free source inventory")
            if (
                self.progress.remaining_source_bytes > 0
                or self.progress.remaining_records > 0
                or self.progress.pending_outbox_count > 0
                or self.progress.blocked_source_count > 0
                or any(not provider.inventory_coverage_complete for provider in self.progress.providers)
            ):
                raise ValueError("current requires complete coverage and no durable work")
        return self

    @classmethod
    def unavailable(cls) -> HistoryImportSnapshot:
        return cls(state="unavailable")
