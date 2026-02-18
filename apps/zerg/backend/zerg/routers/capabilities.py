"""LLM Provider Configuration & System Capabilities API.

Endpoints for:
- GET /system/capabilities — capability status with feature lists
- GET /llm/providers — list configured providers (keys masked)
- PUT /llm/providers/{capability} — upsert provider config
- DELETE /llm/providers/{capability} — remove DB config
- POST /llm/providers/{capability}/test — validate key with minimal API call

All provider endpoints require authentication via ``get_current_user``.
API keys are encrypted at rest with Fernet (same as JobSecret).
"""

from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Response
from fastapi import status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from zerg.config import get_settings
from zerg.database import get_db
from zerg.dependencies.auth import get_current_user
from zerg.models.models import LlmProviderConfig
from zerg.models.models import User
from zerg.utils.crypto import encrypt

logger = logging.getLogger(__name__)

router = APIRouter(tags=["capabilities"])

_settings = get_settings()

# Known provider base URLs (frontend also has these, but backend needs them for test)
_KNOWN_PROVIDERS = {
    "openai": None,  # SDK default
    "groq": "https://api.groq.com/openai/v1",
    "ollama": "http://localhost:11434/v1",
}

# Per-provider test models (used by /test endpoint only)
_TEST_MODELS: dict[str, str] = {
    "openai": "gpt-4o-mini",
    "groq": "llama-3.3-70b-versatile",
    "ollama": "llama3.2",
}

_TEST_EMBEDDING_MODELS: dict[str, str] = {
    "openai": "text-embedding-3-small",
}


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class CapabilityStatus(BaseModel):
    available: bool
    source: str | None = None  # "database", "environment", or None
    provider_name: str | None = None
    features: list[str]


class CapabilitiesResponse(BaseModel):
    text: CapabilityStatus
    embedding: CapabilityStatus


class LlmProviderInfo(BaseModel):
    capability: str
    provider_name: str
    base_url: str | None = None
    source: str = "database"
    has_key: bool = True
    created_at: str | None = None
    updated_at: str | None = None


class LlmProviderUpsertRequest(BaseModel):
    provider_name: str
    api_key: str
    base_url: str | None = None


class LlmProviderTestRequest(BaseModel):
    provider_name: str
    api_key: str
    base_url: str | None = None
    model: str | None = None  # optional: override test model for custom providers


class LlmProviderTestResponse(BaseModel):
    success: bool
    message: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_LOCALHOST_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})


def _validate_base_url(base_url: str | None, provider_name: str) -> str | None:
    """Validate a base_url for SSRF risks.

    Returns an error message string if the URL is blocked, or None if it's OK.
    Ollama is allowed on localhost only; all other providers are blocked from
    private/loopback/reserved IPs and must use HTTPS for remote endpoints.
    """
    if not base_url:
        return None

    import ipaddress
    from urllib.parse import urlparse as _urlparse

    parsed = _urlparse(base_url)
    host = parsed.hostname or ""

    # Ollama: only allow localhost (prevent using "ollama" label to bypass SSRF)
    if provider_name == "ollama":
        if host not in _LOCALHOST_HOSTS:
            return "Ollama provider only allowed on localhost"
        return None

    # Block well-known cloud metadata endpoints
    if host in ("169.254.169.254", "metadata.google.internal"):
        return "Cloud metadata endpoints are blocked"

    # Block loopback
    if host in _LOCALHOST_HOSTS:
        return "Loopback addresses only allowed for Ollama provider"

    # Block private/link-local/loopback/reserved IP ranges
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_private or ip.is_link_local or ip.is_reserved or ip.is_loopback or ip.is_multicast:
            return "Private/reserved IP addresses only allowed for Ollama"
    except ValueError:
        # Hostname — resolve to check for DNS rebinding to private IPs
        import socket

        try:
            addrs = socket.getaddrinfo(host, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
            for _family, _type, _proto, _canonname, sockaddr in addrs:
                resolved_ip = ipaddress.ip_address(sockaddr[0])
                if resolved_ip.is_private or resolved_ip.is_link_local or resolved_ip.is_reserved or resolved_ip.is_loopback:
                    return f"Hostname resolves to private/reserved IP ({sockaddr[0]})"
        except (socket.gaierror, OSError):
            pass  # DNS resolution failed — allow (will fail at connection time)

    # Block http:// (require https for remote endpoints)
    if parsed.scheme == "http":
        return "HTTPS required for remote endpoints"

    return None


def _resolve_capability(capability: str, db: Session, user: User) -> tuple[bool, str | None, str | None]:
    """Check if a capability is available via DB config or env var.

    Returns (available, source, provider_name).
    """
    # Check DB first
    row = db.query(LlmProviderConfig).filter(LlmProviderConfig.owner_id == user.id, LlmProviderConfig.capability == capability).first()
    if row:
        return True, "database", row.provider_name

    # Fall through to env vars
    if capability == "text":
        if os.getenv("OPENAI_API_KEY"):
            return True, "environment", "openai"
        if os.getenv("GROQ_API_KEY"):
            return True, "environment", "groq"
    elif capability == "embedding":
        if os.getenv("OPENAI_API_KEY"):
            return True, "environment", "openai"

    return False, None, None


def _resolve_capability_no_user(capability: str, db: Session) -> tuple[bool, str | None, str | None]:
    """Check capability for single-tenant (any user's DB config or env var)."""
    # Check DB — single-tenant means at most one user
    row = db.query(LlmProviderConfig).filter(LlmProviderConfig.capability == capability).first()
    if row:
        return True, "database", row.provider_name

    # Fall through to env vars
    if capability == "text":
        if os.getenv("OPENAI_API_KEY"):
            return True, "environment", "openai"
        if os.getenv("GROQ_API_KEY"):
            return True, "environment", "groq"
    elif capability == "embedding":
        if os.getenv("OPENAI_API_KEY"):
            return True, "environment", "openai"

    return False, None, None


# ---------------------------------------------------------------------------
# Capabilities endpoint (public — no auth, used by frontend at load)
# ---------------------------------------------------------------------------


@router.get("/capabilities/llm", response_model=CapabilitiesResponse)
def llm_capabilities(
    db: Session = Depends(get_db),
) -> CapabilitiesResponse:
    """Return LLM capability status for text and embedding with feature lists.

    Public endpoint (no auth) so frontend can check at startup.
    Uses env-var check + DB scan (single-tenant: any user's config).
    """
    text_avail, text_src, text_provider = _resolve_capability_no_user("text", db)
    emb_avail, emb_src, emb_provider = _resolve_capability_no_user("embedding", db)

    # Public endpoint: expose availability + features but not provider details
    return CapabilitiesResponse(
        text=CapabilityStatus(
            available=text_avail,
            source=None,  # Don't leak source in public endpoint
            provider_name=None,
            features=["summaries", "reflection", "daily digest", "oikos chat"],
        ),
        embedding=CapabilityStatus(
            available=emb_avail,
            source=None,
            provider_name=None,
            features=["semantic search", "recall", "similar sessions"],
        ),
    )


# ---------------------------------------------------------------------------
# Provider CRUD (authenticated)
# ---------------------------------------------------------------------------


@router.get("/llm/providers", response_model=list[LlmProviderInfo])
def list_llm_providers(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[LlmProviderInfo]:
    """List configured LLM providers for the current user (keys never returned)."""
    rows = db.query(LlmProviderConfig).filter(LlmProviderConfig.owner_id == current_user.id).order_by(LlmProviderConfig.capability).all()
    return [
        LlmProviderInfo(
            capability=row.capability,
            provider_name=row.provider_name,
            base_url=row.base_url,
            source="database",
            has_key=True,
            created_at=row.created_at.isoformat() if row.created_at else None,
            updated_at=row.updated_at.isoformat() if row.updated_at else None,
        )
        for row in rows
    ]


@router.put("/llm/providers/{capability}", status_code=status.HTTP_200_OK)
def upsert_llm_provider(
    capability: str,
    request: LlmProviderUpsertRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    """Create or update an LLM provider config (key encrypted at rest)."""
    if capability not in ("text", "embedding"):
        raise HTTPException(status_code=400, detail="Capability must be 'text' or 'embedding'")

    if not request.api_key:
        raise HTTPException(status_code=400, detail="API key is required")

    # Validate base_url for SSRF before persisting
    ssrf_error = _validate_base_url(request.base_url, request.provider_name)
    if ssrf_error:
        raise HTTPException(status_code=400, detail=ssrf_error)

    encrypted = encrypt(request.api_key)

    existing = (
        db.query(LlmProviderConfig)
        .filter(LlmProviderConfig.owner_id == current_user.id, LlmProviderConfig.capability == capability)
        .first()
    )

    if existing:
        existing.provider_name = request.provider_name
        existing.encrypted_api_key = encrypted
        existing.base_url = request.base_url
        logger.info("Updated LLM provider '%s/%s' for user %d", capability, request.provider_name, current_user.id)
    else:
        config = LlmProviderConfig(
            owner_id=current_user.id,
            capability=capability,
            provider_name=request.provider_name,
            encrypted_api_key=encrypted,
            base_url=request.base_url,
        )
        db.add(config)
        logger.info("Created LLM provider '%s/%s' for user %d", capability, request.provider_name, current_user.id)

    db.commit()
    return {"success": True}


@router.delete("/llm/providers/{capability}", status_code=status.HTTP_204_NO_CONTENT)
def delete_llm_provider(
    capability: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Response:
    """Remove a provider config (reverts to env var fallback)."""
    if capability not in ("text", "embedding"):
        raise HTTPException(status_code=400, detail="Capability must be 'text' or 'embedding'")

    existing = (
        db.query(LlmProviderConfig)
        .filter(LlmProviderConfig.owner_id == current_user.id, LlmProviderConfig.capability == capability)
        .first()
    )
    if not existing:
        raise HTTPException(status_code=404, detail=f"No provider config for '{capability}'")

    db.delete(existing)
    db.commit()
    logger.info("Deleted LLM provider '%s' for user %d", capability, current_user.id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/llm/providers/{capability}/test", response_model=LlmProviderTestResponse)
async def test_llm_provider(
    capability: str,
    request: LlmProviderTestRequest,
    current_user: User = Depends(get_current_user),
) -> LlmProviderTestResponse:
    """Validate an API key with a minimal API call before saving.

    For text: sends a tiny chat completion.
    For embedding: sends a tiny embedding request.
    """
    if capability not in ("text", "embedding"):
        raise HTTPException(status_code=400, detail="Capability must be 'text' or 'embedding'")

    base_url = request.base_url
    if not base_url and request.provider_name in _KNOWN_PROVIDERS:
        base_url = _KNOWN_PROVIDERS[request.provider_name]

    # SSRF mitigation (same validation as upsert)
    ssrf_error = _validate_base_url(base_url, request.provider_name)
    if ssrf_error:
        return LlmProviderTestResponse(success=False, message=ssrf_error)

    try:
        import httpx
        from openai import AsyncOpenAI

        kwargs: dict[str, Any] = {
            "api_key": request.api_key,
            "timeout": httpx.Timeout(10.0, connect=5.0),
        }
        if base_url:
            kwargs["base_url"] = base_url

        client = AsyncOpenAI(**kwargs)

        try:
            if capability == "text":
                test_model = request.model or _TEST_MODELS.get(request.provider_name, "gpt-4o-mini")
                resp = await client.chat.completions.create(
                    model=test_model,
                    messages=[{"role": "user", "content": "Say 'ok'"}],
                    max_tokens=3,
                    extra_body={"metadata": {"source": "longhouse:capabilities-test"}},
                )
                if resp.choices:
                    return LlmProviderTestResponse(success=True, message="Connection successful")
                return LlmProviderTestResponse(success=False, message="No response from API")
            else:
                # Embeddings always use OpenAI-compatible API
                emb_model = request.model or _TEST_EMBEDDING_MODELS.get(request.provider_name, "text-embedding-3-small")
                resp = await client.embeddings.create(
                    model=emb_model,
                    input="test",
                    dimensions=256,
                )
                if resp.data:
                    return LlmProviderTestResponse(success=True, message="Connection successful")
                return LlmProviderTestResponse(success=False, message="No response from API")
        finally:
            await client.close()

    except Exception as e:
        error_msg = str(e)
        if len(error_msg) > 200:
            error_msg = error_msg[:200] + "..."
        return LlmProviderTestResponse(success=False, message=f"Connection failed: {error_msg}")
