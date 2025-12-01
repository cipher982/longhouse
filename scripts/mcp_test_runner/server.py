"""MCP STDIO server exposing a single `run_backend_tests` tool.

This server enables IDE chat agents (Cursor, Copilot-Chat, etc.) to execute the
backend pytest suite programmatically while hiding all repository-specific
details such as *where* to `cd` and which virtual-env command to use.

Transport: STDIO only – the process prints `# mcp:1` once at start-up and then
exchanges newline-delimited JSON messages with the host.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
from typing import Any
from typing import Dict

# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------

# The project root is two levels above this file:  tools/mcp_test_runner/ → tools → *ROOT*
PROJECT_ROOT: pathlib.Path = pathlib.Path(__file__).resolve().parents[2]
BACKEND_ROOT: pathlib.Path = PROJECT_ROOT / "backend"

def run_backend_tests(params: Dict[str, Any]) -> Dict[str, Any]:
    """Run `pytest` on the backend package and return a structured result."""

    path = params.get("path")  # May be *None*

    # Build the command.  We intentionally invoke pytest through **uv** so that
    # dependencies are resolved in the same way as the rest of the repository.
    cmd: list[str] = [
        "uv",
        "run",
        "pytest",
        "-q",  # quiet – we still capture full logs internally
    ]

    if path:
        cmd.append(path)
    else:
        cmd.append("tests")

    proc = subprocess.run(
        cmd,
        cwd=BACKEND_ROOT, # Run pytest from the backend directory
        text=True,
        capture_output=True,
        env={**os.environ, "PYTHONWARNINGS": "ignore"},
    )

    full_output = (proc.stdout or "") + (proc.stderr or "")

    return {
        "passed": proc.returncode == 0,
        "summary": full_output,
    }


# ---------------------------------------------------------------------------
# MCP mainloop
# ---------------------------------------------------------------------------

TOOL_HANDLERS = {"run_backend_tests": run_backend_tests}


def _handle_request(request: Dict[str, Any]) -> Dict[str, Any]:
    """Dispatch a single MCP request and build the JSON response."""

    call_id = request.get("id")
    tool_name = request.get("tool")
    params = request.get("params", {}) or {}

    if tool_name not in TOOL_HANDLERS:
        return {
            "id": call_id,
            "error": {
                "type": "tool_not_found",
                "message": f"No tool named '{tool_name}' is exposed by this MCP server.",
            },
        }

    try:
        result = TOOL_HANDLERS[tool_name](params)
        return {"id": call_id, "result": result}
    except Exception as exc:  # noqa: BLE001 – surface any error back to caller
        return {
            "id": call_id,
            "error": {
                "type": exc.__class__.__name__,
                "message": str(exc),
            },
        }


def main() -> None:  # pragma: no cover – utility entry-point
    # Notify the host that we speak MCP version 1.
    print("# mcp:1", flush=True)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        try:
            request = json.loads(line)
        except json.JSONDecodeError as exc:
            sys.stderr.write(f"[mcp-server] Invalid JSON received: {exc}\n")
            sys.stderr.flush()
            continue

        response = _handle_request(request)
        print(json.dumps(response), flush=True)


if __name__ == "__main__":
    main()
