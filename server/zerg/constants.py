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
AUTOMATIONS_PREFIX = "/automations"
THREADS_PREFIX = "/threads"
MODELS_PREFIX = "/models"


def get_full_path(relative_path: str) -> str:  # noqa: D401 – tiny helper
    """Return absolute API path by joining *relative_path* onto API_PREFIX."""

    return f"{API_PREFIX}{relative_path}"


__all__ = [
    "API_PREFIX",
    "WS_ENDPOINT",
    "AUTOMATIONS_PREFIX",
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
