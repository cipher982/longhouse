from __future__ import annotations

from typing import Literal

from pydantic_settings import BaseSettings
from pydantic_settings import SettingsConfigDict


class Settings(BaseSettings):
    # Core
    admin_token: str
    jwt_secret: str
    database_url: str

    # Google OAuth (control plane login)
    google_client_id: str | None = None
    google_client_secret: str | None = None

    # Stripe
    stripe_secret_key: str | None = None
    stripe_webhook_secret: str | None = None
    stripe_price_id: str | None = None
    stripe_publishable_key: str | None = None

    # Docker/provisioning
    docker_host: str = "unix:///var/run/docker.sock"
    image: str = "ghcr.io/cipher982/longhouse-runtime:latest"
    instance_port: int = 8000
    instance_data_root: str = "/var/app-data/longhouse"
    publish_ports: bool = False

    # Routing/proxy
    root_domain: str = "longhouse.ai"
    proxy_provider: Literal["caddy", "traefik"] = "caddy"
    proxy_network: str = "coolify"
    caddy_tls: str | None = None  # e.g. "internal" or "dns cloudflare"

    # Instance env defaults
    public_site_url: str = "https://longhouse.ai"
    instance_auth_disabled: bool = False
    instance_password: str | None = None
    instance_password_hash: str | None = None
    instance_openai_api_key: str | None = None
    instance_openai_base_url: str | None = None
    instance_openai_allowlist: str | None = None

    # Instance secrets (required when auth is enabled)
    instance_jwt_secret: str
    instance_internal_api_secret: str
    instance_fernet_secret: str
    instance_trigger_signing_secret: str

    # Optional OAuth (instance)
    instance_google_client_id: str | None = None
    instance_google_client_secret: str | None = None
    instance_gmail_pubsub_topic: str | None = None
    instance_pubsub_sa_email: str | None = None

    # Deployment defaults
    deploy_max_parallel: int = 5
    deploy_failure_threshold: int = 3

    # Email (SES) — injected into instances so email works out of the box
    instance_aws_ses_access_key_id: str | None = None
    instance_aws_ses_secret_access_key: str | None = None
    instance_aws_ses_region: str | None = None
    instance_from_email: str | None = None

    # SSH — injected so jobs can SSH to infrastructure hosts
    instance_ssh_private_key_b64: str | None = None

    # LLM keys — injected into every instance (shared pool)
    instance_openrouter_api_key: str | None = None

    # Models routing profile for new instances.
    # "hosted" = OpenRouter-routed defaults (unified provider access)
    # "oss"    = no overrides (expects caller to configure model keys)
    instance_models_profile: str = "hosted"

    # Quota / rate limits injected into every instance.
    # 0 = disabled. Cost limits only bite when pricing catalog has known prices.
    instance_daily_runs_per_user: int = 0
    instance_daily_cost_per_user_cents: int = 0  # per-user daily USD budget in cents
    instance_daily_cost_global_cents: int = 0  # global daily USD budget in cents

    model_config = SettingsConfigDict(env_prefix="CONTROL_PLANE_")


settings = Settings()
