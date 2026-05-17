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
from zerg.models_config import _DB_PROVIDER_DEFAULT_MODELS
from zerg.models_config import EMBEDDING_MODEL
from zerg.models_config import build_openai_compatible_client_kwargs
from zerg.models_config import get_embedding_config
from zerg.models_config import get_provider_default_base_url
from zerg.utils.crypto import decrypt
from zerg.utils.crypto import encrypt

logger = logging.getLogger(__name__)

router = APIRouter(tags=["capabilities"])

_settings = get_settings()

# Known provider base URLs (frontend also has these, but backend needs them for test)
_KNOWN_PROVIDERS = {
    "openai": None,  # SDK default
    "openrouter": get_provider_default_base_url("openrouter"),
    "groq": get_provider_default_base_url("groq"),
    "xai": get_provider_default_base_url("xai"),
    "ollama": "http://localhost:11434/v1",
}

# Per-provider test models — sourced from models_config registry
_TEST_MODELS = _DB_PROVIDER_DEFAULT_MODELS

_TEST_EMBEDDING_MODELS: dict[str, str] = {
    "openai": EMBEDDING_MODEL,
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
    api_key_preview: str | None = None
    source: str = "database"
    has_key: bool = True
    created_at: str | None = None
    updated_at: str | None = None


class LlmProviderUpsertRequest(BaseModel):
    provider_name: str
    api_key: str | None = None
    base_url: str | None = None


class LlmProviderTestRequest(BaseModel):
    provider_name: str
    api_key: str | None = None
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


def _check_env_text_provider() -> tuple[str, str] | None:
    """Find the first text provider with a configured API key via env var.

    Derives the provider list from models.json rather than hardcoding.
    Returns (provider_name, env_var) or None.
    """
    from zerg.models_config import _PROVIDER_DEFAULT_API_KEY_ENVS

    for provider, env_var in _PROVIDER_DEFAULT_API_KEY_ENVS.items():
        if os.getenv(env_var):
            return provider.value, env_var
    return None


def _check_env_embedding_provider() -> tuple[str, str] | None:
    """Return configured env-backed embedding provider from models.json."""
    config = get_embedding_config()
    if config is None:
        return None
    return config.provider, config.api_key_env_var


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
        found = _check_env_text_provider()
        if found:
            return True, "environment", found[0]
    elif capability == "embedding":
        found = _check_env_embedding_provider()
        if found:
            return True, "environment", found[0]

    return False, None, None


def _resolve_capability_no_user(capability: str, db: Session) -> tuple[bool, str | None, str | None]:
    """Check capability for single-tenant (any user's DB config or env var)."""
    # Check DB — single-tenant means at most one user
    row = db.query(LlmProviderConfig).filter(LlmProviderConfig.capability == capability).first()
    if row:
        return True, "database", row.provider_name

    # Fall through to env vars
    if capability == "text":
        found = _check_env_text_provider()
        if found:
            return True, "environment", found[0]
    elif capability == "embedding":
        found = _check_env_embedding_provider()
        if found:
            return True, "environment", found[0]

    return False, None, None


def _default_base_url_for_provider(provider_name: str | None) -> str | None:
    """Return the canonical default base URL for a provider, when known."""
    if not provider_name:
        return None
    return _KNOWN_PROVIDERS.get(provider_name)


def _mask_secret_preview(value: str | None) -> str | None:
    """Show the first and last characters of a secret without exposing the full value."""
    if value is None:
        return None

    trimmed = value.strip()
    if not trimmed:
        return None

    if len(trimmed) <= 2:
        return trimmed

    if len(trimmed) <= 6:
        return f"{trimmed[0]}...{trimmed[-1]}"

    if len(trimmed) <= 8:
        return f"{trimmed[:2]}...{trimmed[-2:]}"

    return f"{trimmed[:4]}...{trimmed[-4:]}"


def _resolve_env_api_key(provider_name: str | None) -> str | None:
    """Resolve an env-backed API key for a known provider."""
    if not provider_name:
        return None

    from zerg.models_config import _PROVIDER_DEFAULT_API_KEY_ENVS

    for provider, env_var in _PROVIDER_DEFAULT_API_KEY_ENVS.items():
        if provider.value == provider_name:
            value = os.getenv(env_var)
            return value.strip() if value and value.strip() else None

    return None


def _safe_api_key_preview_from_row(row: LlmProviderConfig) -> str | None:
    """Best-effort masked preview for a stored encrypted API key."""
    try:
        return _mask_secret_preview(decrypt(row.encrypted_api_key))
    except Exception:
        return None


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
            features=["summaries", "daily digest", "chat"],
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
            api_key_preview=_safe_api_key_preview_from_row(row),
            source="database",
            has_key=True,
            created_at=row.created_at.isoformat() if row.created_at else None,
            updated_at=row.updated_at.isoformat() if row.updated_at else None,
        )
        for row in rows
    ]


@router.get("/llm/providers/effective", response_model=list[LlmProviderInfo])
def list_effective_llm_providers(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[LlmProviderInfo]:
    """List effective provider state for settings UI, including env-backed defaults."""
    db_rows = {
        row.capability: row
        for row in db.query(LlmProviderConfig)
        .filter(LlmProviderConfig.owner_id == current_user.id)
        .order_by(LlmProviderConfig.capability)
        .all()
    }

    result: list[LlmProviderInfo] = []
    for capability in ("embedding", "text"):
        row = db_rows.get(capability)
        if row is not None:
            result.append(
                LlmProviderInfo(
                    capability=row.capability,
                    provider_name=row.provider_name,
                    base_url=row.base_url,
                    api_key_preview=_safe_api_key_preview_from_row(row),
                    source="database",
                    has_key=True,
                    created_at=row.created_at.isoformat() if row.created_at else None,
                    updated_at=row.updated_at.isoformat() if row.updated_at else None,
                )
            )
            continue

        available, source, provider_name = _resolve_capability(capability, db, current_user)
        if not available or source is None or provider_name is None:
            continue

        result.append(
            LlmProviderInfo(
                capability=capability,
                provider_name=provider_name,
                base_url=_default_base_url_for_provider(provider_name),
                api_key_preview=_mask_secret_preview(_resolve_env_api_key(provider_name)),
                source=source,
                has_key=True,
                created_at=None,
                updated_at=None,
            )
        )

    return result


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

    # Validate base_url for SSRF before persisting
    ssrf_error = _validate_base_url(request.base_url, request.provider_name)
    if ssrf_error:
        raise HTTPException(status_code=400, detail=ssrf_error)

    existing = (
        db.query(LlmProviderConfig)
        .filter(LlmProviderConfig.owner_id == current_user.id, LlmProviderConfig.capability == capability)
        .first()
    )

    if existing:
        existing.provider_name = request.provider_name
        if request.api_key:
            existing.encrypted_api_key = encrypt(request.api_key)
        existing.base_url = request.base_url
        logger.info("Updated LLM provider '%s/%s' for user %d", capability, request.provider_name, current_user.id)
    else:
        if not request.api_key:
            raise HTTPException(status_code=400, detail="API key is required")

        config = LlmProviderConfig(
            owner_id=current_user.id,
            capability=capability,
            provider_name=request.provider_name,
            encrypted_api_key=encrypt(request.api_key),
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
    db: Session = Depends(get_db),
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

        api_key = request.api_key
        if not api_key:
            existing = (
                db.query(LlmProviderConfig)
                .filter(
                    LlmProviderConfig.owner_id == current_user.id,
                    LlmProviderConfig.capability == capability,
                )
                .first()
            )
            if existing:
                try:
                    api_key = decrypt(existing.encrypted_api_key)
                except Exception:
                    api_key = None

        if not api_key:
            api_key = _resolve_env_api_key(request.provider_name)

        if not api_key:
            return LlmProviderTestResponse(success=False, message="API key is required to test this connection")

        kwargs: dict[str, Any] = build_openai_compatible_client_kwargs(
            provider=request.provider_name,
            api_key=api_key,
            base_url=base_url,
        )
        kwargs["timeout"] = httpx.Timeout(10.0, connect=5.0)

        client = AsyncOpenAI(**kwargs)

        try:
            if capability == "text":
                test_model = request.model or _TEST_MODELS.get(request.provider_name, _TEST_MODELS["openai"])
                resp = await client.chat.completions.create(
                    model=test_model,
                    messages=[{"role": "user", "content": "Say 'ok'"}],
                )
                if resp.choices:
                    return LlmProviderTestResponse(success=True, message="Connection successful")
                return LlmProviderTestResponse(success=False, message="No response from API")
            else:
                # Embeddings always use OpenAI-compatible API
                emb_model = request.model or _TEST_EMBEDDING_MODELS.get(request.provider_name, EMBEDDING_MODEL)
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
