# Startup Continuity

Status: MVP
Last updated: 2026-04-18

## Goal

When a new Claude or Codex session starts, Longhouse should inject a small,
project-scoped recap of recent work so the model starts with continuity across
sessions and providers.

This is not a standalone briefing product. It is a machine primitive that
improves the launch loop.

## First Principles

- Startup continuity exists for the model, not as a page the human has to read.
- The source of truth is recent project sessions already stored in Longhouse.
- The recap should be small, read-only, and hard to misinterpret as live
  instructions.
- The same backend builder should feed both Claude and Codex.
- Keep v1 narrow: recent session summaries only. No revived insights surface.

## MVP Shape

Backend:

- `GET /api/agents/sessions/startup-context`
- input: `project`, optional bounded `limit` and `days_back`
- output: recent session items plus one rendered `startup_context` block

Selection rules:

- same project only
- summarized sessions only
- writable heads only
- hide sidechains / zero-user-message sessions
- exclude archived sessions
- cross-provider allowed

Delivery:

- Claude `SessionStart` hook fetches the endpoint and emits
  `hookSpecificOutput.additionalContext`
- Codex `SessionStart` hook does the same
- hooks infer the project from the git toplevel basename when available,
  falling back to the raw `cwd` basename

## Non-Goals

- No standalone briefing page
- No cross-project insights bundle
- No extra memory model beyond recent session summaries
- No browser-owned continuity contract
