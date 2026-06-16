"""Shared helpers for hosted tenant auth flows."""

from __future__ import annotations

import os
from urllib.parse import urlparse

from fastapi import HTTPException
from fastapi import status
from zerg.config import get_settings

TENANT_LOGIN_STATE_COOKIE = "tenant_login_state"


def hosted_instance_id() -> str:
    instance_id = os.getenv("INSTANCE_ID", "").strip()
    if instance_id:
        return instance_id

    settings = get_settings()
    public_url = settings.app_public_url or settings.public_site_url or ""
    if public_url:
        host = urlparse(public_url).hostname or ""
        if host:
            return host.split(".")[0]

    raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="INSTANCE_ID is not configured")
