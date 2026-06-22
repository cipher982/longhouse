from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

ProviderActionState = Literal["supported", "read_only", "unknown", "unsupported"]


class ProviderActionProofRefResponse(BaseModel):
    scenario: str
    assertion: str


class ProviderActionCoverageItemResponse(BaseModel):
    id: str
    product_label: str
    state: ProviderActionState
    reason_code: str
    reason: str
    proof_refs: list[ProviderActionProofRefResponse]


class ProviderActionCoverageProviderResponse(BaseModel):
    provider: str
    actions: dict[str, ProviderActionCoverageItemResponse]
    summary: dict[str, int]


class ProviderActionCoverageResponse(BaseModel):
    schema_version: int
    source: str
    states: list[ProviderActionState]
    providers: dict[str, ProviderActionCoverageProviderResponse]
