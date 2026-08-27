"""Hermetic Runtime Host title binding for provider-factory assurance.

This is deliberately a process-startup seam, not a fault-injection API. A
Runtime Host only accepts the binding when all four factory gates are present:
explicit assurance mode, the dedicated candidate environment, a loopback HTTP
endpoint, and an owner-only absolute token file. The Runtime Host stays on its
normal production code paths; the environment is the isolation gate. The path
and transport are immutable for the life of the process; the factory may advance
the token file contents to prove credential-generation recovery without teaching
production how to rotate a credential on command.
"""

from __future__ import annotations

import hashlib
import ipaddress
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from urllib.parse import urlparse

FACTORY_ASSURANCE_MODE_ENV = "LONGHOUSE_FACTORY_ASSURANCE"
FACTORY_ASSURANCE_TITLE_BASE_URL_ENV = "LONGHOUSE_FACTORY_ASSURANCE_TITLE_BASE_URL"
FACTORY_ASSURANCE_TITLE_TOKEN_FILE_ENV = "LONGHOUSE_FACTORY_ASSURANCE_TITLE_TOKEN_FILE"
FACTORY_ASSURANCE_ENVIRONMENT = "candidate-qualification"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _enabled(value: str | None) -> bool:
    return str(value or "").strip().lower() in _TRUTHY


def _loopback_http_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme != "http" or parsed.username or parsed.password or not parsed.hostname or parsed.port is None:
        raise ValueError("factory assurance title base URL must be an explicit loopback HTTP URL with a port")
    try:
        is_loopback = ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        is_loopback = parsed.hostname == "localhost"
    if not is_loopback:
        raise ValueError("factory assurance title base URL must resolve syntactically to loopback")
    return value.rstrip("/")


@dataclass(frozen=True, slots=True)
class FactoryAssuranceTitleBinding:
    base_url: str
    token_file: Path
    credential_binding: str

    def read_token(self) -> str:
        try:
            metadata = self.token_file.lstat()
        except OSError as exc:
            raise ValueError("factory assurance title token file is unavailable") from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise ValueError("factory assurance title token file must be a regular file, not a symlink")
        if metadata.st_mode & 0o077:
            raise ValueError("factory assurance title token file must be owner-only")
        try:
            token = self.token_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ValueError("factory assurance title token file could not be read") from exc
        if not 16 <= len(token.encode("utf-8")) <= 512 or "\n" in token or "\r" in token:
            raise ValueError("factory assurance title token must be one bounded non-empty line")
        return token


def load_factory_assurance_title_binding(
    environment: Mapping[str, str] | None = None,
) -> FactoryAssuranceTitleBinding | None:
    env = os.environ if environment is None else environment
    mode = env.get(FACTORY_ASSURANCE_MODE_ENV)
    base_url = str(env.get(FACTORY_ASSURANCE_TITLE_BASE_URL_ENV) or "").strip()
    token_path = str(env.get(FACTORY_ASSURANCE_TITLE_TOKEN_FILE_ENV) or "").strip()
    configured = _enabled(mode) or bool(base_url) or bool(token_path)
    if not configured:
        return None
    runtime_environment = str(env.get("ENVIRONMENT") or "").strip()
    if not _enabled(mode) or runtime_environment != FACTORY_ASSURANCE_ENVIRONMENT:
        raise ValueError(
            "factory assurance title binding requires explicit assurance mode " f"and ENVIRONMENT={FACTORY_ASSURANCE_ENVIRONMENT}"
        )
    if not base_url or not token_path:
        raise ValueError("factory assurance title binding requires a base URL and token file")
    path = Path(token_path)
    if not path.is_absolute():
        raise ValueError("factory assurance title token file path must be absolute")
    binding = FactoryAssuranceTitleBinding(
        base_url=_loopback_http_url(base_url),
        token_file=path,
        credential_binding=("factory-assurance-title-token-file:sha256:" + hashlib.sha256(os.fsencode(path)).hexdigest()),
    )
    # Fail at process startup, before the Runtime Host claims readiness.
    binding.read_token()
    return binding


# The environment is intentionally captured once. Tests which exercise the
# pure loader pass an explicit mapping; a real Runtime Host gets no mutable
# environment-controlled transport after import.
FACTORY_ASSURANCE_TITLE_BINDING = load_factory_assurance_title_binding()


def factory_assurance_title_binding() -> FactoryAssuranceTitleBinding | None:
    return FACTORY_ASSURANCE_TITLE_BINDING


def factory_assurance_title_enabled() -> bool:
    return FACTORY_ASSURANCE_TITLE_BINDING is not None


__all__ = [
    "FACTORY_ASSURANCE_ENVIRONMENT",
    "FACTORY_ASSURANCE_MODE_ENV",
    "FACTORY_ASSURANCE_TITLE_BASE_URL_ENV",
    "FACTORY_ASSURANCE_TITLE_TOKEN_FILE_ENV",
    "FactoryAssuranceTitleBinding",
    "factory_assurance_title_binding",
    "factory_assurance_title_enabled",
    "load_factory_assurance_title_binding",
]
