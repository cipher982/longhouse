"""Ephemeral container job runner (scaffold).

Provides a minimal API to execute short-lived commands inside a hardened
container with sensible defaults for a multi-tenant SaaS environment.

Notes:
- Rootless: run as non-root UID (default: 65532)
- Network: disabled by default (can be enabled per-invocation later)
- Filesystem: read-only root with optional bind mount for workspace
- Timeouts, CPU and memory limits are supported via Docker options

This module is intentionally self-contained so we can evolve the wiring
without touching agent/workflow code paths until we flip the integration flag.
"""

from __future__ import annotations

import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict
from typing import List
from typing import Optional

from zerg.config import get_settings


class ContainerRunnerError(RuntimeError):
    pass


@dataclass
class ContainerRunnerConfig:
    image: str
    network_disabled: bool
    user_id: int
    memory: str  # e.g. "512m"
    cpus: str  # e.g. "0.5"
    timeout_secs: int
    seccomp_path: Optional[str]

    @staticmethod
    def from_env() -> "ContainerRunnerConfig":
        s = get_settings()
        return ContainerRunnerConfig(
            image=s.container_default_image or "python:3.11-slim",
            network_disabled=not s.container_network_enabled,
            user_id=int(s.container_user_id or 65532),
            memory=s.container_memory_limit or "512m",
            cpus=s.container_cpus or "0.5",
            timeout_secs=int(s.container_timeout_secs or 30),
            seccomp_path=s.container_seccomp_profile or None,
        )


class ContainerRunner:
    """Execute a single command in an ephemeral container."""

    def __init__(self, config: Optional[ContainerRunnerConfig] = None):
        self.config = config or ContainerRunnerConfig.from_env()

        try:  # Import lazily to avoid hard dependency when feature is disabled
            import docker  # type: ignore

            self._docker = docker.from_env()
            self._api = self._docker.api
        except Exception as exc:  # pragma: no cover – environment dependent
            raise ContainerRunnerError(
                "Docker client not available. Install docker SDK and ensure daemon is running."
            ) from exc

    def run(
        self,
        command: List[str],
        *,
        workdir: Optional[Path] = None,
        env: Optional[Dict[str, str]] = None,
        read_write_mount: Optional[Path] = None,
        timeout_secs: Optional[int] = None,
    ) -> Dict[str, object]:
        """Run command and return result dict with exit_code, stdout, stderr, duration_ms."""

        cfg = self.config
        timeout = timeout_secs or cfg.timeout_secs

        # Prepare mounts: optional RW bind to /workspace, read-only root
        volumes: Dict[str, dict] = {}
        tmpdir = None  # Track temp directory for cleanup
        if read_write_mount:
            host_path = str(read_write_mount.resolve())
            volumes[host_path] = {"bind": "/workspace", "mode": "rw"}
            container_workdir = "/workspace"
        else:
            # Provide an isolated workspace when no host bind is provided
            tmpdir = tempfile.mkdtemp(prefix="zerg-run-")
            volumes[tmpdir] = {"bind": "/workspace", "mode": "rw"}
            container_workdir = "/workspace"

        # Security options
        security_opt = ["no-new-privileges:true"]
        host_config_kwargs = {
            "network_mode": "none" if cfg.network_disabled else None,
            "nano_cpus": int(float(cfg.cpus) * 1e9),
            "mem_limit": cfg.memory,
            "security_opt": security_opt,
            "read_only": True,
        }

        if cfg.seccomp_path:
            host_config_kwargs["security_opt"] = security_opt + [f"seccomp={cfg.seccomp_path}"]

        # Create and start container
        start_ts = time.time()
        container = None
        try:
            container = self._docker.containers.create(
                image=cfg.image,
                command=command,
                user=str(cfg.user_id),
                environment=env or {},
                working_dir=container_workdir if workdir is None else str(workdir),
                volumes=volumes,
                stdin_open=False,
                detach=True,
                host_config=self._api.create_host_config(
                    **{k: v for k, v in host_config_kwargs.items() if v is not None}
                ),
            )
            container.start()

            # Wait with timeout; collect combined logs to keep scaffold simple
            res = container.wait(timeout=timeout)
            exit_code = int(res.get("StatusCode", 1))
            logs_bytes = container.logs(stdout=True, stderr=True)
            logs = (
                logs_bytes.decode(errors="replace")
                if isinstance(logs_bytes, (bytes, bytearray))
                else str(logs_bytes)
            )

            duration_ms = int((time.time() - start_ts) * 1000)
            return {
                "exit_code": exit_code,
                "stdout": logs,
                "stderr": "",  # combined logs for now
                "duration_ms": duration_ms,
                "image": cfg.image,
            }
        except Exception as exc:  # noqa: BLE001
            raise ContainerRunnerError(str(exc))
        finally:
            if container is not None:
                try:
                    container.remove(force=True)
                except Exception:
                    pass
            # Clean up temporary workspace directory if it was created
            if tmpdir is not None:
                try:
                    shutil.rmtree(tmpdir, ignore_errors=True)
                except Exception:
                    pass
