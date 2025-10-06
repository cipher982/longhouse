# ---------------------------------------------------------------------------
# NOTE: This module is imported pretty much **everywhere** so we avoid any
# heavyweight dependencies or side-effects here.  Importing
# ``zerg.config.get_settings`` is cheap thanks to the underlying
# ``functools.lru_cache`` but we still guard against circular import issues by
# performing the call *inside* the module scope after the function reference
# is available.
# ---------------------------------------------------------------------------

from typing import Final
from typing import Optional

from zerg.config import get_settings

# Settings instance loaded at module import
_settings = get_settings()

# Base API prefix (all HTTP routes are served under /api/*)
API_PREFIX = "/api"

# WebSocket endpoint – mounted under API_PREFIX
WS_ENDPOINT = "/ws"

# Router prefixes (relative to API_PREFIX)
AGENTS_PREFIX = "/agents"
THREADS_PREFIX = "/threads"
MODELS_PREFIX = "/models"

# ---------------------------------------------------------------------------
# Trigger HMAC signing secret – used by /api/triggers/{id}/events HMAC
# verification (Stage 5 of auth hardening roadmap).
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# In production deployments users *must* provide an explicit
# ``TRIGGER_SIGNING_SECRET``.  Tests, local dev setups or documentation
# generation, however, should not break just because the environment variable
# is missing.  We therefore fall back to a deterministic placeholder when the
# variable is absent **and** the ``TESTING`` flag is set.
# ---------------------------------------------------------------------------

# Resolve trigger signing secret from unified settings.  In *production* the
# value **must** be provided; in *testing* mode we fall back to the deterministic
# value used across the suite so cryptographic checks stay reproducible.

if _settings.trigger_signing_secret is not None:
    TRIGGER_SIGNING_SECRET: Final[str] = _settings.trigger_signing_secret
else:
    if _settings.testing:
        TRIGGER_SIGNING_SECRET = "super-secret-hex"  # noqa: S105 – test only
    else:
        raise KeyError(
            "TRIGGER_SIGNING_SECRET is not set. This secret is required for "
            "secure webhook triggering in production deployments."
        )

# Accept ±5 minutes clock skew for HMAC timestamp header
TRIGGER_TIMESTAMP_TOLERANCE_S: int = 300


def get_full_path(relative_path: str) -> str:  # noqa: D401 – tiny helper
    """Return absolute API path by joining *relative_path* onto API_PREFIX."""

    return f"{API_PREFIX}{relative_path}"


__all__ = [
    "API_PREFIX",
    "WS_ENDPOINT",
    "AGENTS_PREFIX",
    "THREADS_PREFIX",
    "MODELS_PREFIX",
    "get_full_path",
    # Feature flags
    "LLM_TOKEN_STREAM",
]


# ---------------------------------------------------------------------------
# Feature flags (evaluated once at import time)
# ---------------------------------------------------------------------------


# Deprecated helper – retained for backwards-compatibility of *tests* that
# patched feature flags directly.  New code should access values via
# ``settings.<flag>``.


def _env_truthy(name: str, default: Optional[str] = None) -> bool:  # noqa: D401 – legacy
    """Return True if *name* env var is set to a truthy value.

    The function now merely proxies to the canonical Settings instance so the
    semantics remain unchanged while moving away from direct env access.
    """

    return getattr(_settings, name.lower(), False)  # type: ignore[arg-type]


# Public flag exported under the previous constant name so imports stay
# functional while we gradually migrate call-sites.
LLM_TOKEN_STREAM: Final[bool] = _settings.llm_token_stream


# ---------------------------------------------------------------------------
# Test helper – allow reloading env driven flags without re-importing module
# ---------------------------------------------------------------------------


# -----------------------------------------------------------------------
# Test helper – keep backwards compatibility with existing test-suite
# -----------------------------------------------------------------------


def _refresh_feature_flags() -> None:  # pragma: no cover – test helper
    """Reload ``Settings`` from the *current* environment and refresh flags.

    Tests that temporarily monkeypatch ``os.environ`` can call this function
    so the module-level settings instance picks up the changed variables.
    """

    from zerg.config import get_settings as _get_settings  # local import

    # Fetch a fresh settings instance
    global _settings  # noqa: PLW0603 – module global
    _settings = _get_settings()

    global LLM_TOKEN_STREAM  # noqa: PLW0603 – module global
    LLM_TOKEN_STREAM = _settings.llm_token_stream


__all__.append("_refresh_feature_flags")
