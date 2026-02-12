"""Runtime context injected into jobs that declare ``run(ctx: JobContext)``.

Jobs with ``run()`` (no params) keep working via ``os.environ`` for backwards
compatibility.  Jobs that accept a ``JobContext`` get only the secrets they
declared in ``JobConfig.secrets``, already decrypted and ready to use.
"""

from __future__ import annotations


class JobContext:
    """Runtime context injected into jobs that declare ``run(ctx: JobContext)``."""

    __slots__ = ("_job_id", "_secrets")

    def __init__(self, job_id: str, secrets: dict[str, str]) -> None:
        self._job_id = job_id
        self._secrets = secrets  # Only declared secrets, already decrypted

    @property
    def job_id(self) -> str:
        return self._job_id

    def require_secret(self, key: str) -> str:
        """Get a secret by key. Raises ``RuntimeError`` if not available."""
        if key not in self._secrets:
            raise RuntimeError(f"Secret '{key}' not available for job '{self._job_id}'. " f"Declared: {sorted(self._secrets.keys())}")
        return self._secrets[key]

    def get_secret(self, key: str, default: str | None = None) -> str | None:
        """Get a secret by key, returning *default* if not found."""
        return self._secrets.get(key, default)

    @property
    def secrets(self) -> dict[str, str]:
        """Read-only view of all declared secrets."""
        return dict(self._secrets)


__all__ = ["JobContext"]
