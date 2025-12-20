# MCP Test Runner

This directory contains a **local‐only [Model-Context Protocol](https://github.com/model-context)** (MCP) server that exposes a
single tool – **`run_backend_tests`** – for executing the backend `pytest`
suite in a predictable way from IDE chat agents (e.g. Cursor or Copilot-Chat).

Why do we need it?

- Chat agents struggle with the exact shell incantation needed to run tests
  because our repository relies on **`uv run`**, has its tests inside a nested
  `apps/zerg/backend/` folder, and sometimes requires passing a _specific_ test path.
- By delegating the work to an MCP server we hide those details and give the
  agent a single structured call that _always works_.

## File overview

| File            | Purpose                                                                                 |
| --------------- | --------------------------------------------------------------------------------------- |
| `server.py`     | Python process that speaks MCP over **STDIO** and handles the `run_backend_tests` call. |
| `manifest.json` | Machine-readable description of the tool – consumed by MCP hosts.                       |

## Quick start (for humans)

```bash
# From the repository root
python scripts/mcp_test_runner/server.py

# You should immediately see the header:
# mcp:1

# 👉  The process now waits for newline-delimited JSON requests on stdin.  For
# a manual test you could open another terminal and do:
# simple stdin test:
echo '{"id":"1","tool":"run_backend_tests","params":{}}' | python scripts/mcp_test_runner/server.py
```

In practice you **never** have to talk to the server by hand – modern IDEs
spawn it automatically and route tool calls through it.

## Protocol example

Request:

```jsonc
{
  "id": "42",
  "tool": "run_backend_tests",
  "params": { "path": "tests/test_models.py::test_ok" },
}
```

Response:

```jsonc
{
  "id": "42",
  "result": {
    "passed": true,
    "summary": "=================== 1 passed in 0.04s ===================",
  },
}
```

---

If you need additional tools (linting, migrations, etc.) simply extend
`TOOL_HANDLERS` in `server.py` and update `manifest.json` accordingly.
