#!/usr/bin/env python3
"""Internal entrypoint; the host launcher supplies only an explicit JSON environment."""

import json
import os
import subprocess
from pathlib import Path


def main() -> None:
    config = Path("/tmp/test-env.json")
    environment = json.loads(config.read_text())
    config.unlink()
    os.environ.clear()
    os.environ.update(
        {
            "PATH": "/work/server/.venv/bin:/usr/local/cargo/bin:/usr/local/bin:/usr/bin:/bin",
            "RUSTUP_HOME": "/usr/local/rustup",
            "CARGO_HOME": "/usr/local/cargo",
            "UV_OFFLINE": "1",
            "UV_NO_SYNC": "1",
            "CARGO_NET_OFFLINE": "true",
            "UV_CACHE_DIR": "/opt/uv-cache",
            "BUN_INSTALL_CACHE_DIR": "/opt/bun-cache",
            "PLAYWRIGHT_BROWSERS_PATH": "/opt/playwright",
            "LANG": "C.UTF-8",
            **environment,
        }
    )
    for key in ("HOME", "TMPDIR", "LONGHOUSE_HOME"):
        Path(os.environ[key]).mkdir(parents=True, exist_ok=True)
    Path("/tmp/longhouse-test-isolated").touch()
    subprocess.run(["git", "init", "-q"], check=True)
    subprocess.run(["git", "add", "--all"], check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Longhouse Test",
            "-c",
            "user.email=test@invalid",
            "commit",
            "-qm",
            "Isolated test snapshot",
        ],
        check=True,
    )
    subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            "/work/server/.venv/bin/python",
            "--no-deps",
            "--no-build-isolation",
            "-e",
            "/work/server",
        ],
        check=True,
    )
    argv = json.loads(os.environ.pop("LONGHOUSE_TEST_COMMAND"))
    os.execvpe(argv[0], argv, os.environ)


if __name__ == "__main__":
    main()
