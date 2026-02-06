from __future__ import annotations

import hashlib
import os
import secrets
import time
from dataclasses import dataclass
from dataclasses import field

import docker
import httpx

from control_plane.config import settings


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


def _env_for(subdomain: str, owner_email: str, password: str | None = None) -> dict[str, str]:
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
        "CONTROL_PLANE_JWT_SECRET": settings.jwt_secret,
        "INTERNAL_API_SECRET": settings.instance_internal_api_secret,
        "FERNET_SECRET": settings.instance_fernet_secret,
        "TRIGGER_SIGNING_SECRET": settings.instance_trigger_signing_secret,
    }

    if password:
        env["LONGHOUSE_PASSWORD"] = password

    return env


def _volume_for(subdomain: str) -> tuple[str, dict[str, str]]:
    data_path = os.path.join(settings.instance_data_root, subdomain)
    os.makedirs(data_path, exist_ok=True)
    # Set ownership to UID 1000 (longhouse user inside container) when possible.
    if os.geteuid() == 0:
        os.chown(data_path, 1000, 1000)
    else:
        os.chmod(data_path, 0o777)
    return data_path, {data_path: {"bind": "/data", "mode": "rw"}}


class Provisioner:
    def __init__(self):
        self.client = docker.DockerClient(base_url=settings.docker_host)

    def ensure_network(self, container):
        if not settings.proxy_network:
            return
        network = self.client.networks.get(settings.proxy_network)
        network.connect(container)

    def provision_instance(
        self,
        subdomain: str,
        owner_email: str,
        *,
        password: str | None = None,
    ) -> ProvisionResult:
        container_name = f"longhouse-{subdomain}"

        existing = self.client.containers.list(all=True, filters={"name": container_name})
        if existing:
            container = existing[0]
            return ProvisionResult(container_name=container.name, data_path="")

        # Use provided password or generate a new one
        if password:
            password_hash: str | None = None  # caller handles hashing
        else:
            password, password_hash = _generate_password()

        data_path, volumes = _volume_for(subdomain)
        labels = _labels_for(subdomain)
        env = _env_for(subdomain, owner_email, password=password)

        ports = None
        if settings.publish_ports:
            ports = {settings.instance_port: settings.instance_port}

        container = self.client.containers.run(
            image=settings.image,
            name=container_name,
            detach=True,
            labels=labels,
            environment=env,
            volumes=volumes,
            ports=ports,
        )

        self.ensure_network(container)

        return ProvisionResult(
            container_name=container.name,
            data_path=data_path,
            password=password,
            password_hash=password_hash,
        )

    def deprovision_instance(self, container_name: str) -> None:
        try:
            container = self.client.containers.get(container_name)
        except docker.errors.NotFound:
            return
        container.stop(timeout=20)
        container.remove()

    def wait_for_health(self, subdomain: str, timeout: int = 120) -> bool:
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

        raise RuntimeError(f"Health check failed for {host}: {last_error}")
