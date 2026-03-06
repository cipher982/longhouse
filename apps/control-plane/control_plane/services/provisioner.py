from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import time
from dataclasses import dataclass
from dataclasses import field

import docker
import httpx

from control_plane.config import settings

logger = logging.getLogger(__name__)

_ENV_KEY_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")
_CORE_OWNED_ENV_KEYS = {
    "INSTANCE_ID",
    "OWNER_EMAIL",
    "ADMIN_EMAILS",
    "SINGLE_TENANT",
    "AUTH_DISABLED",
    "APP_PUBLIC_URL",
    "PUBLIC_SITE_URL",
    "DATABASE_URL",
    "JWT_SECRET",
    "CONTROL_PLANE_JWT_SECRET",
    "INTERNAL_API_SECRET",
    "FERNET_SECRET",
    "TRIGGER_SIGNING_SECRET",
    "CONTROL_PLANE_URL",
    "LONGHOUSE_PASSWORD",
}


def _generate_fernet_key() -> str:
    """Generate a throwaway URL-safe base64-encoded 32-byte key (Fernet-compatible)."""
    import base64

    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()


def _generate_password() -> tuple[str, str]:
    """Generate a random password and its PBKDF2 hash.

    Returns (plaintext, hash_string).
    """
    password = secrets.token_urlsafe(24)
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 600_000)
    hash_string = f"pbkdf2:sha256:600000${salt.hex()}${dk.hex()}"
    return password, hash_string


@dataclass
class ProvisionResult:
    container_name: str
    data_path: str
    password: str | None = field(default=None)
    password_hash: str | None = field(default=None)
    image: str | None = field(default=None)
    image_digest: str | None = field(default=None)


def _host_for(subdomain: str) -> str:
    return f"{subdomain}.{settings.root_domain}"


def _labels_for(subdomain: str) -> dict[str, str]:
    host = _host_for(subdomain)
    labels: dict[str, str] = {}

    if settings.proxy_provider == "traefik":
        labels.update(
            {
                "traefik.enable": "true",
                f"traefik.http.routers.{subdomain}.rule": f"Host(`{host}`)",
                f"traefik.http.routers.{subdomain}.entrypoints": "websecure",
                f"traefik.http.routers.{subdomain}.tls": "true",
                f"traefik.http.services.{subdomain}.loadbalancer.server.port": str(settings.instance_port),
            }
        )
    else:
        # caddy-docker-proxy labels
        labels["caddy"] = host
        labels["caddy.reverse_proxy"] = f"{{{{upstreams {settings.instance_port}}}}}"
        if settings.caddy_tls:
            labels["caddy.tls"] = settings.caddy_tls

    return labels


def _openai_allowlist() -> set[str]:
    raw = settings.instance_openai_allowlist
    if not raw:
        return set()
    return {item.strip().lower() for item in raw.split(",") if item.strip()}


def _openai_env_allowed(subdomain: str, owner_email: str) -> bool:
    allowlist = _openai_allowlist()
    if not allowlist:
        return False
    if "*" in allowlist:
        return True
    return subdomain.lower() in allowlist or owner_email.lower() in allowlist


def normalize_custom_env_overrides(overrides: dict[str, object] | None) -> dict[str, str | None]:
    """Validate and normalize per-instance env overrides.

    Rules:
    - keys must match POSIX-like env format (`^[A-Z][A-Z0-9_]*$`)
    - core-owned keys are forbidden
    - values must be string or null (null removes the key from merged env)
    """
    if not overrides:
        return {}

    normalized: dict[str, str | None] = {}
    for raw_key, raw_value in overrides.items():
        if not isinstance(raw_key, str):
            raise ValueError("custom env keys must be strings")
        key = raw_key.strip()
        if not key:
            raise ValueError("custom env key cannot be empty")
        if key in _CORE_OWNED_ENV_KEYS:
            raise ValueError(f"{key} is managed by control plane and cannot be overridden")
        if not _ENV_KEY_PATTERN.match(key):
            raise ValueError(f"invalid env key: {key}")
        if raw_value is None:
            normalized[key] = None
            continue
        if not isinstance(raw_value, str):
            raise ValueError(f"custom env value for {key} must be a string or null")
        normalized[key] = raw_value
    return normalized


def parse_custom_env_json(raw: str | None) -> dict[str, str | None]:
    """Parse persisted custom env JSON strictly.

    Raises ValueError on invalid JSON/payload so callers fail loudly.
    """
    if not raw:
        return {}
    try:
        decoded = json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        raise ValueError("invalid custom env JSON") from exc
    if not isinstance(decoded, dict):
        raise ValueError("custom env JSON must decode to an object")
    return normalize_custom_env_overrides(decoded)


def _apply_custom_env(env: dict[str, str], custom_env: dict[str, str | None] | None) -> dict[str, str]:
    if not custom_env:
        return env

    merged = dict(env)
    for key, value in custom_env.items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = value
    return merged


def _env_for(
    subdomain: str,
    owner_email: str,
    password: str | None = None,
    *,
    custom_env: dict[str, str | None] | None = None,
) -> dict[str, str]:
    env: dict[str, str] = {
        "INSTANCE_ID": subdomain,
        "OWNER_EMAIL": owner_email,
        "ADMIN_EMAILS": owner_email,
        "SINGLE_TENANT": "1",
        "AUTH_DISABLED": "1" if settings.instance_auth_disabled else "0",
        "APP_PUBLIC_URL": f"https://{_host_for(subdomain)}",
        "PUBLIC_SITE_URL": settings.public_site_url,
        "DATABASE_URL": "sqlite:////data/longhouse.db",
        "JWT_SECRET": settings.instance_jwt_secret,
        "CONTROL_PLANE_JWT_SECRET": settings.instance_jwt_secret,
        "INTERNAL_API_SECRET": settings.instance_internal_api_secret,
        "FERNET_SECRET": settings.instance_fernet_secret,
        "TRIGGER_SIGNING_SECRET": settings.instance_trigger_signing_secret,
        "CONTROL_PLANE_URL": f"https://control.{settings.root_domain}",
    }

    if password:
        env["LONGHOUSE_PASSWORD"] = password

    # NOTE: Google OAuth is NOT passed to instances — Google doesn't support
    # wildcard redirect URIs, so *.longhouse.ai can't use Google OAuth directly.
    # Instead, users authenticate via the control plane (which owns the OAuth
    # client) and get SSO'd into their instance via /auth/sso?token=xxx.

    # Email (SES) — platform-provided so jobs can send email out of the box
    if settings.instance_aws_ses_access_key_id and settings.instance_aws_ses_secret_access_key:
        env["AWS_SES_ACCESS_KEY_ID"] = settings.instance_aws_ses_access_key_id
        env["AWS_SES_SECRET_ACCESS_KEY"] = settings.instance_aws_ses_secret_access_key
        if settings.instance_aws_ses_region:
            env["AWS_SES_REGION"] = settings.instance_aws_ses_region
        if settings.instance_from_email:
            env["FROM_EMAIL"] = settings.instance_from_email
        # NOTIFY_EMAIL = the instance owner's email (they get their own notifications)
        env["NOTIFY_EMAIL"] = owner_email

    if _openai_env_allowed(subdomain, owner_email):
        if settings.instance_openai_api_key:
            env["OPENAI_API_KEY"] = settings.instance_openai_api_key
        if settings.instance_openai_base_url:
            env["OPENAI_BASE_URL"] = settings.instance_openai_base_url

    if settings.instance_ssh_private_key_b64:
        env["SSH_PRIVATE_KEY_B64"] = settings.instance_ssh_private_key_b64

    # LLM shared pool — Groq is injected into every instance (no allowlist needed;
    # Groq pricing is cheap enough to share and we enforce quotas via the limit vars below).
    if settings.instance_groq_api_key:
        env["GROQ_API_KEY"] = settings.instance_groq_api_key

    # Models routing profile — determines default tiers for shared-pool users.
    env["MODELS_PROFILE"] = settings.instance_models_profile

    # Quota / rate limits — all default to 0 (disabled) until the operator sets them.
    # BYO-key users are automatically exempt; these only bite shared-pool users.
    if settings.instance_daily_runs_per_user > 0:
        env["DAILY_RUNS_PER_USER"] = str(settings.instance_daily_runs_per_user)
    if settings.instance_daily_cost_per_user_cents > 0:
        env["DAILY_COST_PER_USER_CENTS"] = str(settings.instance_daily_cost_per_user_cents)
    if settings.instance_daily_cost_global_cents > 0:
        env["DAILY_COST_GLOBAL_CENTS"] = str(settings.instance_daily_cost_global_cents)

    return _apply_custom_env(env, custom_env)


def _data_path_for(subdomain: str, *, data_path: str | None = None) -> str:
    return data_path or os.path.join(settings.instance_data_root, subdomain)


def _ensure_data_path(data_path: str) -> None:
    os.makedirs(data_path, exist_ok=True)
    # Set ownership to UID 1000 (longhouse user inside container) when possible.
    if os.geteuid() == 0:
        os.chown(data_path, 1000, 1000)
    else:
        os.chmod(data_path, 0o777)


def _volume_for(subdomain: str, *, data_path: str | None = None) -> tuple[str, dict[str, dict[str, str]]]:
    target_data_path = _data_path_for(subdomain, data_path=data_path)
    _ensure_data_path(target_data_path)
    return target_data_path, {target_data_path: {"bind": "/data", "mode": "rw"}}


class Provisioner:
    def __init__(self):
        self.client = docker.DockerClient(base_url=settings.docker_host)

    def ensure_network(self, container):
        if not settings.proxy_network:
            return
        network = self.client.networks.get(settings.proxy_network)
        network.connect(container)

    def run_migration_preflight(self, subdomain: str, *, data_path: str | None = None) -> str:
        """Apply heavy DB migrations against instance data before reprovision.

        Runs a one-shot container on the target data volume:
        `python -m zerg.cli.main migrate --database-url sqlite:////data/longhouse.db --apply --json`
        """
        target_data_path, volumes = _volume_for(subdomain, data_path=data_path)

        command = [
            "python",
            "-m",
            "zerg.cli.main",
            "migrate",
            "--database-url",
            "sqlite:////data/longhouse.db",
            "--apply",
            "--json",
        ]

        try:
            output = self.client.containers.run(
                image=settings.image,
                command=command,
                remove=True,
                volumes=volumes,
                environment={
                    "DATABASE_URL": "sqlite:////data/longhouse.db",
                    "AUTH_DISABLED": "1",
                    # Migration doesn't use Fernet but the settings validator requires it.
                    # Generate a throwaway key — schema migrations never read encrypted data.
                    "FERNET_SECRET": _generate_fernet_key(),
                },
                extra_hosts={"host.docker.internal": "host-gateway"},
            )
        except docker.errors.ContainerError as exc:
            stderr_value = getattr(exc, "stderr", None)
            stdout_value = getattr(exc, "stdout", None)
            stderr = stderr_value.decode("utf-8", errors="replace") if isinstance(stderr_value, (bytes, bytearray)) else ""
            stdout = stdout_value.decode("utf-8", errors="replace") if isinstance(stdout_value, (bytes, bytearray)) else ""
            detail = "\n".join(part for part in [stderr, stdout] if part).strip() or str(exc)
            raise RuntimeError(f"Migration preflight failed for {subdomain}: {detail}") from exc

        if isinstance(output, (bytes, bytearray)):
            return output.decode("utf-8", errors="replace")
        return str(output or "")

    def provision_instance(
        self,
        subdomain: str,
        owner_email: str,
        *,
        password: str | None = None,
        custom_env: dict[str, str | None] | None = None,
        data_path: str | None = None,
        image: str | None = None,
        skip_pull: bool = False,
    ) -> ProvisionResult:
        container_name = f"longhouse-{subdomain}"
        use_image = image or settings.image

        try:
            container = self.client.containers.get(container_name)
            return ProvisionResult(container_name=container.name, data_path=_data_path_for(subdomain, data_path=data_path))
        except docker.errors.NotFound:
            pass

        # Use provided password or generate a new one
        if password:
            password_hash: str | None = None  # caller handles hashing
        else:
            password, password_hash = _generate_password()

        data_path, volumes = _volume_for(subdomain, data_path=data_path)
        labels = _labels_for(subdomain)
        env = _env_for(subdomain, owner_email, password=password, custom_env=custom_env)

        ports = None
        if settings.publish_ports:
            ports = {settings.instance_port: settings.instance_port}

        # Pull image before provisioning (skip if batch deploy already pre-pulled)
        if not skip_pull:
            try:
                self.client.images.pull(use_image)
            except docker.errors.ImageNotFound:
                # Allow local-only tags (CI/dev) when the image exists locally.
                try:
                    self.client.images.get(use_image)
                    logger.warning("Image %s not found in registry; using local image", use_image)
                except docker.errors.ImageNotFound:
                    raise

        container = self.client.containers.run(
            image=use_image,
            name=container_name,
            detach=True,
            labels=labels,
            environment=env,
            volumes=volumes,
            ports=ports,
            extra_hosts={"host.docker.internal": "host-gateway"},
        )

        self.ensure_network(container)

        # Resolve image digest for immutable reference
        image_digest = None
        try:
            pulled = self.client.images.get(use_image)
            digests = pulled.attrs.get("RepoDigests", [])
            if digests:
                image_digest = digests[0]
        except Exception:  # noqa: BLE001
            pass

        return ProvisionResult(
            container_name=container.name,
            data_path=data_path,
            password=password,
            password_hash=password_hash,
            image=use_image,
            image_digest=image_digest,
        )

    def deprovision_instance(self, container_name: str) -> None:
        try:
            container = self.client.containers.get(container_name)
        except docker.errors.NotFound:
            return
        container.stop(timeout=20)
        container.remove()

    def wait_for_health(self, subdomain: str, timeout: int = 120) -> bool:
        if settings.publish_ports:
            url = f"http://127.0.0.1:{settings.instance_port}/api/health"
        else:
            host = _host_for(subdomain)
            url = f"https://{host}/api/health"
        deadline = time.time() + timeout
        last_error = None

        while time.time() < deadline:
            try:
                resp = httpx.get(url, timeout=5.0, follow_redirects=True)
                if resp.status_code == 200:
                    return True
                last_error = f"status={resp.status_code}"
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
            time.sleep(2)

        target = _host_for(subdomain)
        raise RuntimeError(f"Health check failed for {target}: {last_error}")
