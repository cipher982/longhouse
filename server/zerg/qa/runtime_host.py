"""Verifier-owned Runtime Host child process.

Scenarios that judge the product start the real Runtime Host (`zerg.main:app`
under uvicorn) as a child with auth off and an isolated data root, then talk to
it over HTTP. The verifier never imports the product code it judges.
"""

from __future__ import annotations

import base64
import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path

import httpx


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def ephemeral_fernet_secret() -> str:
    """Return one process-local encryption key for a hermetic Runtime Host."""

    return base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")


class RuntimeHost:
    """One isolated Runtime Host child, owned and stopped by the caller."""

    def __init__(
        self,
        *,
        repo_root: Path,
        root: Path,
        instance_id: str,
        extra_env: Mapping[str, str] | None = None,
        drop_env: Sequence[str] = (),
        database_name: str = "runtime-live.db",
    ) -> None:
        self.repo_root = repo_root
        self.root = root
        self.instance_id = instance_id
        self.port = free_port()
        self.api_url = f"http://127.0.0.1:{self.port}"
        self.database_path = root / database_name
        self.log_path = root / "runtime.log"
        self.extra_env = dict(extra_env or {})
        self.drop_env = tuple(drop_env)
        self.process: subprocess.Popen[bytes] | None = None
        self._log_stream = None

    def _child_environment(self, server_root: Path) -> dict[str, str]:
        environment = dict(os.environ)
        # The verifier owns every Runtime Host dependency. Never borrow a host
        # or factory credential: inherited values make the check non-hermetic
        # and can leak a durable secret into a child that needs only an
        # ephemeral encryption key.
        environment.pop("FERNET_SECRET", None)
        environment.update(
            {
                "DATABASE_URL": f"sqlite:///{self.root / 'runtime.db'}",
                "FERNET_SECRET": ephemeral_fernet_secret(),
                "TESTING": "1",
                "AUTH_DISABLED": "1",
                "SINGLE_TENANT": "1",
                "INSTANCE_ID": self.instance_id,
                "LONGHOUSE_DATA_DIR": str(self.root / "data"),
                "LONGHOUSE_HOME": str(self.root / "home"),
                "LONGHOUSE_STORAGE_V2_ROOT": str(self.root / "storage-v2"),
                "E2E_LOG_SUPPRESS": "0",
                "LOG_LEVEL": "INFO",
            }
        )
        environment.update(self.extra_env)
        for name in self.drop_env:
            environment.pop(name, None)
        environment["PYTHONPATH"] = os.pathsep.join(item for item in (str(server_root), environment.get("PYTHONPATH", "")) if item)
        return environment

    def start(self) -> None:
        if self.process is not None:
            raise RuntimeError("Runtime Host is already running")
        server_root = self.repo_root / "server"
        environment = self._child_environment(server_root)
        self._log_stream = self.log_path.open("ab")
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "zerg.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(self.port),
                "--no-access-log",
            ],
            cwd=server_root,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=self._log_stream,
            stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + 45
        # One client for the whole poll: a bare httpx.get builds a new client, and with it a TLS context
        # that loads the CA bundle (about 30 ms of CPU), on every 0.1 s poll of a plain-HTTP local port.
        with httpx.Client(timeout=1) as client:
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise RuntimeError(f"Runtime Host exited during startup with {self.process.returncode}")
                try:
                    response = client.get(f"{self.api_url}/health")
                    if response.status_code < 500:
                        return
                except httpx.HTTPError:
                    pass
                time.sleep(0.1)
        raise TimeoutError("Runtime Host did not become reachable")

    def stop(self) -> int | None:
        process = self.process
        if process is None:
            return None
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        returncode = process.returncode
        self.process = None
        if self._log_stream is not None:
            self._log_stream.close()
            self._log_stream = None
        return returncode

    def catalogd_socket(self, *, timeout_seconds: float = 60.0) -> Path:
        """Wait for the host's own catalogd socket and return its path.

        Keep ``root`` short (under /tmp): a Unix socket path is limited to 104 bytes.
        """

        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            found = next(iter(self.root.rglob("catalogd.sock")), None)
            if found is not None:
                return found
            time.sleep(0.1)
        raise TimeoutError("Runtime Host catalogd socket did not appear")

    def __enter__(self) -> RuntimeHost:
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()
