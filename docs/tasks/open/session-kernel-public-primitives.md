# Session Kernel and Public Primitives

Status: In progress
Last updated: 2026-04-02

## Goal

Make Longhouse's session kernel and coordination surfaces the canonical product seam: durable sessions, CLI/API-first public primitives, MCP as an adapter, and Oikos reduced to a thin operator layer on top of the kernel instead of a competing durable runtime model.

## End State

- The durable object in Longhouse is the session. "Agent" remains useful product language, but operationally it is an ephemeral wrapper around work.
- The machine-facing canon is explicit and stable: HTTP first, CLI second, MCP on top.
- Coordination works without MCP: discover peers, inspect tails, send messages, acknowledge inbox items, and continue work from terminal or API.
- Longhouse remains the integrated distribution bundling timeline, continuity, managed-local control, engine/shipper, runner, and Oikos.
- Oikos behaves like a bounded operator/deputy connected to the platform, not a second brain with parallel durable state.

## Done when

- `/api/agents/*` is the documented machine namespace for session, coordination, continuity, and message flows.
- CLI parity exists for the core session/coordination primitives, with machine-readable `--json` output.
- Session messaging supports durable queueing, acknowledgement, safe-boundary delivery, and a documented fallback for non-live sessions.
- Coordination workflows are documented and tested as CLI/API-first flows that do not require MCP.
- Oikos can inspect, message, continue, and summarize sessions through canonical primitives instead of owning parallel durability.
- The integrated product story is clearer, but no repo extraction or separate service split is required to get there.

## Checklist

- [x] Update `VISION.md` to formalize session kernel, CLI/API-first public primitives, MCP-as-adapter, and Longhouse as the integrated distribution
- [x] Land the coordination read-side foundation locally (`wall`, `tail`, `peers` across HTTP/MCP)
- [x] Add `SessionMessage` persistence, acknowledgement, safe-boundary managed-local delivery, and E2E coverage
- [x] Add the first coordination CLI commands: `longhouse peers` and `longhouse message`
- [ ] Declare the canonical machine surface in docs: `/api/agents/*`, auth model, session-context headers, and JSON contracts
- [ ] Add the remaining coordination/session CLI parity: `longhouse tail`, `longhouse sessions get`, `longhouse sessions events`
- [ ] Add CLI inbox helpers for non-live sessions: `check-messages` / `ack-message` or equivalent
- [ ] Decide and document the browser/timeline relationship to the machine canon (browser veneer vs direct reuse)
- [ ] Expand `SessionMessage` delivery beyond the current managed-local fast path
- [ ] Decide whether queued delivery should drain multiple messages at one safe boundary or intentionally stay one-at-a-time
- [ ] Add machine-contract tests and CLI smoke coverage for the canonical primitives
- [ ] Make Oikos consume canonical primitives as an operator/deputy layer
- [ ] Split oversized routers/services only where it materially improves contract ownership or unblocks feature work

## Notes

- This task owns the kernel/coordination canon. It should not block unrelated product slices.
- Oikos cleanup is parallel work, not a prerequisite for shipping coordination primitives.
- Router splitting is housekeeping unless it unblocks a real contract or delivery problem.
- Do not build a universal interop framework here. A2A/AGNTCY-style adapters are later, optional layers.
- Do not extract new repos or services yet. First make the primitives obviously canonical inside Longhouse.
- Current delivery trigger is presence-driven safe-boundary delivery. That is the right default and should stay simple.
- Known gap: queued delivery currently attempts one message per deliverable presence update.
- Current CLI progress: `peers` and `message` now hit the canonical `/api/agents/*` machine routes directly and are covered by backend tests.
- Related active tasks:
  - `docs/tasks/open/launch-runtime-simplification.md`
  - `docs/tasks/open/oikos-proactive-operator.md`
