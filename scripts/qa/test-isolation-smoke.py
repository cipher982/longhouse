#!/usr/bin/env python3
"""Exercise the portable fixture boundary from inside the actual test process."""

import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path


def main() -> int:
    root = Path(os.environ["LONGHOUSE_TEST_ROOT"])
    assert Path.home().is_relative_to(root), "HOME escaped the disposable root"
    for key in (
        "CODEX_HOME",
        "CLAUDE_CONFIG_DIR",
        "LONGHOUSE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
    ):
        assert Path(os.environ[key]).is_relative_to(root), key
    for key in (
        "OPENAI_API_KEY",
        "CURSOR_API_KEY",
        "INFISICAL_TOKEN",
        "SSH_AUTH_SOCK",
        "LONGHOUSE_MACHINE_TOKEN",
        "AWS_ACCESS_KEY_ID",
    ):
        assert not os.environ.get(key), f"ambient authority inherited: {key}"
    for name in ("cursor-agent", "codex", "claude", "security", "hatch"):
        assert shutil.which(name) is None, f"host executable leaked: {name}"
    assert not Path("/var/run/docker.sock").exists(), "host Docker authority leaked"
    assert not Path("/Users").exists(), "host home directories leaked"
    assert not Path(".env.test").exists(), "private dotenv copied into source"
    # Network none retains *this container's* loopback, not the host's.
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        with socket.create_connection(listener.getsockname(), timeout=2) as client:
            peer, _ = listener.accept()
            with peer:
                client.sendall(b"isolated")
                assert peer.recv(8) == b"isolated"
    try:
        socket.create_connection(("1.1.1.1", 443), timeout=1)
    except OSError:
        pass
    else:
        raise AssertionError("fixture process has external network access")
    # A descendant intentionally outlives this process. Container destruction,
    # not successful parent exit, must reap it (verified by the outer receipt).
    subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(600)"],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    print(
        json.dumps(
            {
                "status": "pass",
                "home": str(Path.home()),
                "loopback": "isolated",
                "egress": "denied",
                "provider_binaries": "absent",
                "descendant_cleanup": "outer-container-owned",
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
