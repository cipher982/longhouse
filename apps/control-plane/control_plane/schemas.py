from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class InstanceCreate(BaseModel):
    email: str
    subdomain: str


class InstanceOut(BaseModel):
    id: int
    email: str
    subdomain: str
    container_name: str
    status: str
    created_at: datetime | None = None
    last_health_at: datetime | None = None


class InstanceList(BaseModel):
    instances: list[InstanceOut]


class TokenOut(BaseModel):
    token: str
    expires_in: int
