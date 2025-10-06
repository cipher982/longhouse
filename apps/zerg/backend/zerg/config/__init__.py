"""Centralised configuration helper (no external dependencies).

This module eliminates scattered ``os.getenv`` calls by exposing a **single**
process-wide :class:`Settings` instance (retrieved via :func:`get_settings`).

We intentionally **avoid** a runtime dependency on *pydantic-settings* to keep
the core backend lightweight – a bespoke implementation is less than 100 LOC
while covering all current needs.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

# ``_REPO_ROOT`` points to the top-level repository directory (one level
# **above** the "backend" package).  We use ``parents[3]`` because this file is
# located at ``backend/zerg/config/__init__.py``.

_REPO_ROOT = Path(__file__).resolve().parents[3]


def _truthy(value: str | None) -> bool:  # noqa: D401 – small helper
    """Return *True* when *value* looks like an affirmative string."""

    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:  # noqa: D401 – simple data container
    """Lightweight settings container populated from environment variables."""

    # Runtime flags -----------------------------------------------------
    testing: bool
    auth_disabled: bool

    # Secrets & IDs -----------------------------------------------------
    jwt_secret: str
    google_client_id: Any
    google_client_secret: Any
    trigger_signing_secret: Any

    # Database ---------------------------------------------------------
    database_url: str

    # Cryptography -----------------------------------------------------
    fernet_secret: Any

    # Feature flags ----------------------------------------------------
    _llm_token_stream_default: bool  # internal default

    # Misc
    dev_admin: bool
    log_level: str
    e2e_log_suppress: bool
    environment: Any
    allowed_cors_origins: str
    openai_api_key: Any
    # Public URL --------------------------------------------------------
    app_public_url: str | None
    # Pub/Sub OIDC audience --------------------------------------------
    pubsub_audience: str | None
    gmail_pubsub_topic: str | None
    pubsub_sa_email: str | None
    # User/account limits ----------------------------------------------
    max_users: int
    admin_emails: str  # comma-separated list
    # LLM limits --------------------------------------------------------
    max_output_tokens: int
    # Model policy ------------------------------------------------------
    allowed_models_non_admin: str  # csv list
    # Quotas ------------------------------------------------------------
    daily_runs_per_user: int
    # Cost budgets (in cents) ------------------------------------------
    daily_cost_per_user_cents: int
    daily_cost_global_cents: int

    # Discord alerts/digest (ops)
    discord_webhook_url: str | None
    discord_enable_alerts: bool
    discord_daily_digest_cron: str

    # Database reset security
    db_reset_password: str | None

    # Dynamic guards (evaluated at runtime) -----------------------------
    @property
    def llm_disabled(self) -> bool:  # noqa: D401
        """Return True when outbound LLM calls are globally disabled."""
        return _truthy(os.getenv("LLM_DISABLED"))

    # ------------------------------------------------------------------
    # Dynamic feature helper – evaluates *each time* so tests that tweak the
    # env var at runtime still propagate.
    # ------------------------------------------------------------------

    @property
    def llm_token_stream(self) -> bool:  # noqa: D401 – convenience
        return _truthy(os.getenv("LLM_TOKEN_STREAM")) or self._llm_token_stream_default

    # Helper for tests to override values at runtime -------------------
    def override(self, **kwargs: Any) -> None:  # pragma: no cover – test util
        for key, value in kwargs.items():
            if not hasattr(self, key):  # pragma: no cover – safety
                raise AttributeError(f"Settings has no attribute '{key}'")
            setattr(self, key, value)


# ---------------------------------------------------------------------------
# Singleton accessor – values loaded only once per interpreter
# ---------------------------------------------------------------------------


def _load_settings() -> Settings:  # noqa: D401 – helper
    """Populate :class:`Settings` from environment variables."""

    testing = _truthy(os.getenv("TESTING"))

    # ------------------------------------------------------------------
    # Load environment files using python-dotenv (proper parsing)
    # ------------------------------------------------------------------

    # Load environment file based on NODE_ENV
    node_env = os.getenv("NODE_ENV", "development")

    if node_env == "test":
        env_path = _REPO_ROOT / ".env.test"
        if not env_path.exists():
            env_path = _REPO_ROOT / ".env"  # Fallback to main .env
    else:
        env_path = _REPO_ROOT / ".env"

    if env_path.exists():
        # Preserve ENVIRONMENT and TESTING if explicitly set for E2E test isolation
        # (E2E tests set these to specific values which should not be overridden by .env)
        current_env = os.getenv("ENVIRONMENT")
        current_testing = os.getenv("TESTING")
        is_e2e_test_env = current_env and ("test" in current_env or "e2e" in current_env)

        load_dotenv(env_path, override=True)  # Project .env is authoritative for development

        # Restore E2E test environment variables if they were explicitly set
        if is_e2e_test_env:
            os.environ["ENVIRONMENT"] = current_env
        if current_testing:  # Preserve TESTING if it was explicitly set
            os.environ["TESTING"] = current_testing

    return Settings(
        testing=testing,
        auth_disabled=_truthy(os.getenv("AUTH_DISABLED")) or testing,
        jwt_secret=os.getenv("JWT_SECRET", "dev-secret"),
        google_client_id=os.getenv("GOOGLE_CLIENT_ID"),
        google_client_secret=os.getenv("GOOGLE_CLIENT_SECRET"),
        trigger_signing_secret=os.getenv("TRIGGER_SIGNING_SECRET"),
        database_url=os.getenv("DATABASE_URL", ""),
        fernet_secret=os.getenv("FERNET_SECRET"),
        _llm_token_stream_default=_truthy(os.getenv("LLM_TOKEN_STREAM")),
        dev_admin=_truthy(os.getenv("DEV_ADMIN")),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        e2e_log_suppress=_truthy(os.getenv("E2E_LOG_SUPPRESS")),
        environment=os.getenv("ENVIRONMENT"),
        allowed_cors_origins=os.getenv("ALLOWED_CORS_ORIGINS", ""),
        openai_api_key=os.getenv("OPENAI_API_KEY"),
        app_public_url=os.getenv("APP_PUBLIC_URL"),
        pubsub_audience=os.getenv("PUBSUB_AUDIENCE"),
        gmail_pubsub_topic=os.getenv("GMAIL_PUBSUB_TOPIC"),
        pubsub_sa_email=os.getenv("PUBSUB_SA_EMAIL"),
        max_users=int(os.getenv("MAX_USERS", "20")),
        admin_emails=os.getenv("ADMIN_EMAILS", os.getenv("ADMIN_EMAIL", "")),
        max_output_tokens=int(os.getenv("MAX_OUTPUT_TOKENS", "1000")),
        allowed_models_non_admin=os.getenv("ALLOWED_MODELS_NON_ADMIN", ""),
        daily_runs_per_user=int(os.getenv("DAILY_RUNS_PER_USER", "0")),
        daily_cost_per_user_cents=int(os.getenv("DAILY_COST_PER_USER_CENTS", "0")),
        daily_cost_global_cents=int(os.getenv("DAILY_COST_GLOBAL_CENTS", "0")),
        discord_webhook_url=os.getenv("DISCORD_WEBHOOK_URL"),
        discord_enable_alerts=_truthy(os.getenv("DISCORD_ENABLE_ALERTS")),
        discord_daily_digest_cron=os.getenv("DISCORD_DAILY_DIGEST_CRON", "0 8 * * *"),
        db_reset_password=os.getenv("DB_RESET_PASSWORD"),
    )


# ------------------------------------------------------------------
# Runtime validation – fail fast when *required* secrets are missing.
# ------------------------------------------------------------------


def _validate_required(settings: Settings) -> None:  # noqa: D401 – helper
    """Abort startup when mandatory configuration is missing.

    The application should never reach runtime with an absent
    ``OPENAI_API_KEY`` (unless the *TESTING* flag is active).  Performing the
    check here – immediately after loading the environment – surfaces
    mis-configurations early and prevents a cascade of HTTP connection errors
    once the first LLM call is made.
    """

    if settings.testing:  # Unit-/integration tests run with stubbed LLMs
        return

    # Critical configuration validation - fail fast on missing required vars
    missing_vars = []

    # Core application requirements
    if not settings.openai_api_key:
        missing_vars.append("OPENAI_API_KEY")

    if not settings.database_url:
        missing_vars.append("DATABASE_URL")

    # Encryption requirements
    if not settings.fernet_secret:
        missing_vars.append("FERNET_SECRET")

    # Authentication requirements
    if not settings.auth_disabled:
        weak = settings.jwt_secret.strip() in {"", "dev-secret"} or len(settings.jwt_secret) < 16
        if weak:
            missing_vars.append("JWT_SECRET (must be >=16 chars, not 'dev-secret')")

        if not settings.google_client_id:
            missing_vars.append("GOOGLE_CLIENT_ID (required when auth enabled)")

        if not settings.google_client_secret:
            missing_vars.append("GOOGLE_CLIENT_SECRET (required when auth enabled)")

    if missing_vars:
        error_msg = (
            f"CRITICAL: Missing required environment variables: {', '.join(missing_vars)}\n"
            f"Set these in your .env file or deployment environment.\n"
            f"Current DATABASE_URL: '{settings.database_url}'\n"
            f"Current OPENAI_API_KEY: '{'SET' if settings.openai_api_key else 'MISSING'}'\n"
            f"Deployment will fail without these variables."
        )
        raise RuntimeError(error_msg)


def get_settings() -> Settings:  # noqa: D401 – public accessor
    """Return :class:`Settings` instance loaded from environment."""

    settings = _load_settings()
    _validate_required(settings)
    return settings


__all__ = [
    "Settings",
    "get_settings",
]
