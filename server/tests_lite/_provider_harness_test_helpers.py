from __future__ import annotations

import sys
from pathlib import Path


def install_fake_engine(path: Path) -> Path:
    """Install a production-shaped permission-canary engine test double."""
    path.write_text(
        f"""#!{sys.executable}
import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
assert args[0] == "codex-app-server-canary"
assert "--auto-approve" in args
assert (Path(os.environ["HOME"]) / ".codex").is_dir()
codex_bin = Path(args[args.index("--codex-bin") + 1])
assert codex_bin.is_file() and codex_bin.stat().st_mode & 0o100
print(json.dumps({{
    "turn_status": "completed",
    "server_request_counts": {{
        "item/commandExecution/requestApproval": 1,
        "item/permissions/requestApproval": 1,
        "item/tool/requestUserInput": 1,
    }},
    "thread_active_flag_counts": {{
        "waitingOnApproval": 1,
        "waitingOnUserInput": 1,
    }},
    "response_errors": [],
}}))
""",
        encoding="utf-8",
    )
    path.chmod(0o700)
    return path
