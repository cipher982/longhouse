"""MCP STDIO server exposing trace debugging tools for Zerg.

This server enables IDE chat agents (Cursor, Claude Code, etc.) to debug
Zerg agent runs by querying traces across all tables (agent_runs, worker_jobs,
llm_audit_log) with a single trace_id.

Workflow:
1. User copies trace_id from Jarvis chat UI footer
2. User asks AI: "debug trace abc-123"
3. AI calls this MCP tool → gets full context
4. AI explains what happened

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

# The project root is two levels above this file: scripts/mcp_debug_trace/ → scripts → *ROOT*
PROJECT_ROOT: pathlib.Path = pathlib.Path(__file__).resolve().parents[2]
BACKEND_ROOT: pathlib.Path = PROJECT_ROOT / "apps" / "zerg" / "backend"


def debug_trace(params: Dict[str, Any]) -> Dict[str, Any]:
    """Debug a trace by showing the full timeline across all tables.

    Args:
        trace_id: UUID of the trace to debug (required)
        level: Detail level - 'summary' (default), 'full', or 'errors'

    Returns:
        JSON with trace timeline, anomalies, and stats
    """
    trace_id = params.get("trace_id")
    if not trace_id:
        return {"error": "trace_id is required"}

    level = params.get("level", "summary")
    if level not in ("summary", "full", "errors"):
        level = "summary"

    cmd: list[str] = [
        "uv",
        "run",
        "python",
        "scripts/debug_trace.py",
        trace_id,
        "--level",
        level,
        "--json",
    ]

    proc = subprocess.run(
        cmd,
        cwd=BACKEND_ROOT,
        text=True,
        capture_output=True,
        env={**os.environ, "PYTHONWARNINGS": "ignore"},
        timeout=60,  # Generous timeout for DB queries
    )

    if proc.returncode != 0:
        error_output = (proc.stderr or proc.stdout or "").strip()
        return {
            "success": False,
            "error": error_output or "debug_trace.py failed with no output",
        }

    # Parse JSON output from the script
    try:
        result = json.loads(proc.stdout)
        return {
            "success": True,
            **result,
        }
    except json.JSONDecodeError as e:
        return {
            "success": False,
            "error": f"Failed to parse debug_trace output: {e}",
            "raw_output": proc.stdout[:1000],
        }


def list_recent_traces(params: Dict[str, Any]) -> Dict[str, Any]:
    """List recent traces for discovery.

    Args:
        limit: Maximum number of traces to return (default: 20, max: 100)

    Returns:
        JSON with list of recent traces and their basic info
    """
    try:
        limit = min(int(params.get("limit", 20)), 100)
    except (TypeError, ValueError):
        limit = 20

    # Use a Python one-liner to query recent traces
    # This avoids needing to modify debug_trace.py for JSON output
    python_code = f"""
import json
from zerg.database import get_session_factory
from zerg.models.models import AgentRun

SessionLocal = get_session_factory()
db = SessionLocal()

try:
    runs = (
        db.query(AgentRun)
        .filter(AgentRun.trace_id.isnot(None))
        .order_by(AgentRun.created_at.desc())
        .limit({limit})
        .all()
    )

    result = []
    for run in runs:
        result.append({{
            "trace_id": str(run.trace_id) if run.trace_id else None,
            "run_id": run.id,
            "status": run.status.value if run.status else None,
            "model": run.model,
            "started_at": run.started_at.isoformat() if run.started_at else None,
            "finished_at": run.finished_at.isoformat() if run.finished_at else None,
            "error": run.error[:100] if run.error else None,
        }})

    print(json.dumps({{"traces": result}}))
finally:
    db.close()
"""

    cmd: list[str] = ["uv", "run", "python", "-c", python_code]

    proc = subprocess.run(
        cmd,
        cwd=BACKEND_ROOT,
        text=True,
        capture_output=True,
        env={**os.environ, "PYTHONWARNINGS": "ignore"},
        timeout=30,
    )

    if proc.returncode != 0:
        error_output = (proc.stderr or proc.stdout or "").strip()
        return {
            "success": False,
            "error": error_output or "Query failed with no output",
        }

    try:
        result = json.loads(proc.stdout)
        return {
            "success": True,
            **result,
        }
    except json.JSONDecodeError as e:
        return {
            "success": False,
            "error": f"Failed to parse output: {e}",
            "raw_output": proc.stdout[:1000],
        }


# ---------------------------------------------------------------------------
# MCP mainloop
# ---------------------------------------------------------------------------

TOOL_HANDLERS = {
    "debug_trace": debug_trace,
    "list_recent_traces": list_recent_traces,
}

# Tool metadata for MCP discovery (JSON Schema format)
TOOL_METADATA = {
    "debug_trace": {
        "description": "Debug a Zerg agent trace by showing the full timeline across runs, workers, and LLM calls",
        "parameters": {
            "trace_id": {"type": "string", "description": "UUID of the trace to debug"},
            "level": {
                "type": "string",
                "enum": ["summary", "full", "errors"],
                "default": "summary",
                "description": "Detail level: summary (overview), full (with LLM details), errors (anomalies only)",
            },
        },
        "required": ["trace_id"],
    },
    "list_recent_traces": {
        "description": "List recent Zerg agent traces for discovery",
        "parameters": {
            "limit": {
                "type": "integer",
                "default": 20,
                "description": "Maximum number of traces to return (1-100)",
            },
        },
        "required": [],
    },
}


def _handle_request(request: Dict[str, Any]) -> Dict[str, Any]:
    """Dispatch a single MCP request and build the JSON response."""

    call_id = request.get("id")
    method = request.get("method")

    # Handle tools/list request
    if method == "tools/list":
        tools = []
        for name, meta in TOOL_METADATA.items():
            schema = {
                "type": "object",
                "properties": meta["parameters"],
            }
            if meta.get("required"):
                schema["required"] = meta["required"]
            tools.append(
                {
                    "name": name,
                    "description": meta["description"],
                    "inputSchema": schema,
                }
            )
        return {"id": call_id, "result": {"tools": tools}}

    # Handle tool invocation (legacy format)
    tool_name = request.get("tool")
    if not tool_name:
        # Try MCP 2024-11-05 format: method="tools/call", params.name, params.arguments
        if method == "tools/call":
            params = request.get("params", {})
            tool_name = params.get("name")
            tool_params = params.get("arguments", {})
        else:
            return {
                "id": call_id,
                "error": {
                    "type": "invalid_request",
                    "message": "Missing 'tool' or 'method' field in request",
                },
            }
    else:
        tool_params = request.get("params", {}) or {}

    if tool_name not in TOOL_HANDLERS:
        return {
            "id": call_id,
            "error": {
                "type": "tool_not_found",
                "message": f"No tool named '{tool_name}' is exposed by this MCP server. Available: {list(TOOL_HANDLERS.keys())}",
            },
        }

    try:
        result = TOOL_HANDLERS[tool_name](tool_params)
        return {"id": call_id, "result": result}
    except subprocess.TimeoutExpired:
        return {
            "id": call_id,
            "error": {
                "type": "timeout",
                "message": f"Tool '{tool_name}' timed out",
            },
        }
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
            sys.stderr.write(f"[mcp-debug-trace] Invalid JSON received: {exc}\n")
            sys.stderr.flush()
            continue

        response = _handle_request(request)
        print(json.dumps(response), flush=True)


if __name__ == "__main__":
    main()
