"""Explicit isolated authentication for stock Codex CLI runs.

Stock Codex does not treat Longhouse's internal ``CODEX_API_KEY`` binding as a
login operation.  The supported non-interactive path is ``codex login
--with-api-key`` with the key on stdin.  Keep that detail in one helper so
every qualification lane uses the same provider-native auth boundary.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Mapping


class CodexAuthError(RuntimeError):
    """The isolated Codex profile could not be authenticated."""


def login_with_api_key(
    binary: Path,
    *,
    api_key: str,
    environment: Mapping[str, str],
    cwd: Path,
    timeout: float = 30.0,
) -> dict[str, str]:
    """Log stock Codex into a fresh ``CODEX_HOME`` without ambient auth.

    The key is supplied only through stdin.  It is intentionally absent from
    the child environment so a provider cannot silently choose an unsupported
    environment-based fallback.  The returned receipt contains no secret.
    """

    secret = api_key.strip()
    if not secret:
        raise CodexAuthError("Codex API key is empty")
    raw_home = str(environment.get("CODEX_HOME") or "").strip()
    if not raw_home:
        raise CodexAuthError("CODEX_HOME is required for isolated Codex login")
    codex_home = Path(raw_home).expanduser()
    if not codex_home.is_absolute():
        raise CodexAuthError("CODEX_HOME must be absolute for isolated Codex login")
    try:
        codex_home.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as exc:
        raise CodexAuthError("cannot create isolated CODEX_HOME") from exc

    child_environment = dict(environment)
    child_environment.pop("CODEX_API_KEY", None)
    try:
        result = subprocess.run(
            [str(binary), "login", "--with-api-key"],
            cwd=cwd,
            env=child_environment,
            input=f"{secret}\n",
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CodexAuthError(f"Codex isolated login failed: {type(exc).__name__}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").replace(secret, "[REDACTED]").strip()
        raise CodexAuthError(f"Codex isolated login failed ({result.returncode}): {detail[:240]}")

    auth_path = codex_home / "auth.json"
    if not auth_path.is_file():
        raise CodexAuthError("Codex login completed without creating CODEX_HOME/auth.json")
    try:
        auth_path.chmod(0o600)
    except OSError as exc:
        raise CodexAuthError("Codex auth.json permissions could not be restricted") from exc
    return {
        "method": "codex_login_with_api_key_stdin",
        "auth_path": str(auth_path),
        "environment_key_used": "none",
    }


__all__ = ["CodexAuthError", "login_with_api_key"]
