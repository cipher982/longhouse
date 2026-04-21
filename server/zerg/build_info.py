"""Longhouse build identity loader.

Single path: `importlib.resources.files("zerg") / "build_identity.json"`.

Every Python install mode stages the same file into the `zerg` package:
- Source runs / editable installs / `make dev` — `scripts/build/generate_build_identity.py`
  writes `server/zerg/build_identity.json` directly. No env var.
- Wheel / sdist / Docker image — built on top of the same staged file.

If the resource is missing, raise `BuildIdentityMissing`. No fallback,
no guessing, no env-var override.

Display format (see docs/specs/release-and-build-identity.md):
- release channel:    "0.2.0 (b672fcca)"
- dev channel, clean: "0.2.0-dev+b672fcca"
- dev channel, dirty: "0.2.0-dev+b672fcca.dirty"
"""

from __future__ import annotations

import json
from dataclasses import asdict
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources

RESOURCE_PACKAGE = "zerg"
RESOURCE_NAME = "build_identity.json"


class BuildIdentityMissing(RuntimeError):
    """Raised when build identity cannot be located. Always loud, never silent."""


@dataclass(frozen=True)
class BuildIdentity:
    version: str
    commit: str
    commit_short: str
    dirty: bool
    built_at: str
    channel: str

    @property
    def qualified_version(self) -> str:
        if self.channel == "release":
            return f"{self.version} ({self.commit_short})"
        suffix = f"{self.commit_short}.dirty" if self.dirty else self.commit_short
        return f"{self.version}-dev+{suffix}"

    def as_dict(self) -> dict:
        return asdict(self)


_ALLOWED_CHANNELS = ("dev", "release")


def _parse(raw: str, source: str) -> BuildIdentity:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BuildIdentityMissing(f"build identity at {source} is not valid JSON: {exc}") from exc
    try:
        version = payload["version"]
        commit = payload["commit"]
        commit_short = payload["commit_short"]
        dirty = payload["dirty"]
        built_at = payload["built_at"]
        channel = payload["channel"]
    except KeyError as exc:
        raise BuildIdentityMissing(f"build identity at {source} missing key {exc}") from exc
    if not isinstance(dirty, bool):
        raise BuildIdentityMissing(f"build identity at {source} has non-bool dirty={dirty!r}")
    if channel not in _ALLOWED_CHANNELS:
        raise BuildIdentityMissing(f"build identity at {source} has invalid channel={channel!r}; expected one of {_ALLOWED_CHANNELS}")
    for name, value in (("version", version), ("commit", commit), ("commit_short", commit_short), ("built_at", built_at)):
        if not isinstance(value, str) or not value:
            raise BuildIdentityMissing(f"build identity at {source} has invalid {name}={value!r}")
    return BuildIdentity(
        version=version,
        commit=commit,
        commit_short=commit_short,
        dirty=dirty,
        built_at=built_at,
        channel=channel,
    )


def _load_from_resource() -> BuildIdentity:
    try:
        ref = resources.files(RESOURCE_PACKAGE) / RESOURCE_NAME
    except ModuleNotFoundError as exc:
        raise BuildIdentityMissing(f"package {RESOURCE_PACKAGE!r} not found") from exc
    if not ref.is_file():
        raise BuildIdentityMissing(
            f"bundled resource {RESOURCE_PACKAGE}/{RESOURCE_NAME} is missing. " "Run scripts/build/generate_build_identity.py to stage it."
        )
    return _parse(ref.read_text(encoding="utf-8"), source=f"{RESOURCE_PACKAGE}/{RESOURCE_NAME}")


@lru_cache(maxsize=1)
def load() -> BuildIdentity:
    """Load the build identity for this process. Cached for the process lifetime."""
    return _load_from_resource()


def reset_cache() -> None:
    """Clear the cached identity. Tests only."""
    load.cache_clear()
