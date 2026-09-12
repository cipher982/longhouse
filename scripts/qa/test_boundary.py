#!/usr/bin/env python3
"""Recognize a live isolated invocation, not a stale global marker.

The container or hosted VM is the isolation boundary. This private child-only
record prevents ordinary host commands from accidentally bypassing dispatch.
"""

from __future__ import annotations

import json
import os
import secrets
import stat
import sys
from collections.abc import MutableMapping
from pathlib import Path


def create_boundary(root: Path, environment: MutableMapping[str, str]) -> None:
    container = sys.platform == "linux" and Path("/.dockerenv").is_file()
    native = (
        sys.platform == "darwin"
        and os.environ.get("RUNNER_ENVIRONMENT") == "github-hosted"
        and os.environ.get("GITHUB_ACTIONS") == "true"
    )
    if not (container or native):
        raise RuntimeError("test boundaries can only be created in an isolated worker")
    token = secrets.token_hex(32)
    path = root / f"boundary-{token}.json"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump({"token": token, "pid": os.getpid(), "platform": sys.platform}, stream)
    environment["LONGHOUSE_TEST_BOUNDARY"] = str(path)
    environment["LONGHOUSE_TEST_BOUNDARY_NONCE"] = token
    environment["LONGHOUSE_TEST_ISOLATED"] = "1"


def boundary_active() -> bool:
    path = os.environ.get("LONGHOUSE_TEST_BOUNDARY")
    token = os.environ.get("LONGHOUSE_TEST_BOUNDARY_NONCE")
    if not path or not token:
        return False
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor) as stream:
            metadata = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or metadata.st_mode & 0o077
            ):
                return False
            record = json.load(stream)
        if (
            record.get("platform") != sys.platform
            or not secrets.compare_digest(record.get("token", ""), token)
            or type(record.get("pid")) is not int
            or record["pid"] <= 1
        ):
            return False
        os.kill(record["pid"], 0)
        return True
    except (OSError, ValueError, TypeError, AttributeError):
        return False


if __name__ == "__main__":
    raise SystemExit(0 if boundary_active() else 1)
