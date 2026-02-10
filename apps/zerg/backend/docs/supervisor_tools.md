# Oikos Tools

## Status

- **Canonical source of truth:** `apps/zerg/backend/zerg/tools/builtin/oikos_tools.py`
- **Companion architecture spec:** `apps/zerg/backend/docs/specs/unified-memory-bridge.md`
- **Doc status:** current as of 2026-02-10

This document replaces older milestone-era descriptions that referenced removed components (e.g. `CommisRunner`) or non-existent test/doc paths.

## Purpose

Oikos is the coordinator layer. Its tools are for:

1. Delegating substantial work to CLI agents (commis)
2. Tracking commis lifecycle + reading commis artifacts
3. Running lightweight direct actions (search/memory/web/messaging) without delegation when appropriate

## Tool Groups

### 1) Delegation + Commis Lifecycle

Primary delegation and lifecycle tools:

- `spawn_workspace_commis` (primary)
- `spawn_commis` (legacy compatibility alias)
- `list_commiss`
- `check_commis_status`
- `wait_for_commis`
- `cancel_commis`
- `read_commis_result`
- `get_commis_metadata`
- `peek_commis_output`
- `grep_commiss`
- `read_commis_file`
- `get_commis_evidence`
- `get_tool_output`

### 2) Session Selection UX

- `request_session_selection`

This opens the session picker flow for resume/discovery UX when a user has not provided a specific session ID.

### 3) Oikos Utility Allowlist

In addition to the Oikos-specific tools above, Oikos receives utility tools from `OIKOS_UTILITY_TOOLS` via `get_oikos_allowed_tools()`.

Current utility categories include:

- Time (`get_current_time`)
- Web (`web_search`, `web_fetch`, `http_request`)
- Communication (`send_email`)
- Knowledge (`knowledge_search`)
- Memory (`save_memory`, `search_memory`, `list_memories`, `forget_memory`)
- Session discovery (`search_sessions`, `grep_sessions`, `filter_sessions`, `get_session_detail`)
- Runner management (`runner_list`, `runner_create_enroll_token`)

## Current Runtime Semantics (Important)

- Commis run through workspace-mode CLI execution (`hatch` subprocess).
- Standard/in-process commis mode is deprecated and being removed.
- `spawn_workspace_commis` is the intended path for new behavior.
- `spawn_commis` exists for compatibility and should not be treated as a long-term API contract.

## Alignment Work In Progress

From first-principles review (2026-02-10), docs/prompt/runtime had drift around delegation semantics.

Target contract (tracked in the spec):

1. Oikos dispatches by intent: direct answer vs quick tool vs CLI delegation.
2. Backend intent should be explicit (Claude/Codex/Gemini), not hidden.
3. Delegation modes should be explicit (repo workspace vs scratch workspace).

See `apps/zerg/backend/docs/specs/unified-memory-bridge.md` Phase 3 for implementation details.

## Tests

Primary tests touching Oikos tools and commis orchestration:

- `apps/zerg/backend/tests/tools/test_oikos_tools_errors.py`
- `apps/zerg/backend/tests/test_supervisor_service.py`
- `apps/zerg/e2e/tests/core/commis-simplification.spec.ts`
- `apps/zerg/e2e/tests/core/commis-flow.spec.ts`

Use `make test` / `make test-e2e` from repo root.
