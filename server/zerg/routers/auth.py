"""Tenant auth router aggregator."""

from __future__ import annotations

from fastapi import APIRouter

from zerg.auth.session_tokens import JWT_SECRET
from zerg.auth.session_tokens import _encode_jwt
from zerg.routers import auth_browser
from zerg.routers import auth_sso

router = APIRouter()
router.include_router(auth_browser.router)
router.include_router(auth_sso.router)

__all__ = [
    "JWT_SECRET",
    "_encode_jwt",
    "router",
]
