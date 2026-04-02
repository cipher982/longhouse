# Session Kernel and Public Primitives

Status: In progress
Spec: `docs/specs/agents-machine-surface.md`
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
- [x] Add `longhouse tail` backed by the canonical session tail route
- [x] Add CLI inbox helpers for non-live sessions: `check-messages` / `ack-message`
- [x] Add `longhouse sessions get` and `longhouse sessions events`
- [x] Declare the canonical machine surface in docs: `/api/agents/*`, auth model, session-context headers, and JSON contracts
- [x] Decide and document the browser/timeline relationship to the machine canon (browser veneer vs direct reuse)
- [ ] Expand `SessionMessage` delivery beyond the current managed-local fast path
- [x] Decide whether queued delivery should drain multiple messages at one safe boundary or intentionally stay one-at-a-time
- [x] Add machine-contract tests and CLI smoke coverage for the canonical primitives
- [x] Make Oikos consume canonical primitives as an operator/deputy layer
- [x] Split oversized routers/services only where it materially improves contract ownership or unblocks feature work

## Notes

- This task owns the kernel/coordination canon. It should not block unrelated product slices.
- Oikos cleanup is parallel work, not a prerequisite for shipping coordination primitives.
- Router splitting is housekeeping unless it unblocks a real contract or delivery problem.
- Do not build a universal interop framework here. A2A/AGNTCY-style adapters are later, optional layers.
- Do not extract new repos or services yet. First make the primitives obviously canonical inside Longhouse.
- Current delivery trigger is presence-driven safe-boundary delivery. That is the right default and should stay simple.
- Queued delivery now drains up to 10 messages per safe-boundary wakeup, but stops immediately if the target session leaves a deliverable state.
- Current CLI progress: `wall`, `peers`, `message`, `tail`, `check-messages`, `ack-message`, `sessions get`, and `sessions events` now hit the canonical `/api/agents/*` machine routes directly and are covered by backend tests.
- Machine-surface test coverage now includes the canonical `POST /api/agents/insights` write path plus JSON smoke coverage for the core coordination/session CLI commands.
- Canonical machine surface is now documented in `docs/specs/agents-machine-surface.md`, including the browser veneer split and the `POST /api/agents/insights` canonical write path.
- `docs/specs/agents-machine-surface.md` now includes copyable HTTP and CLI recipes for the raw wall, directed session messaging, inbox reads, and acknowledgements.
- The raw wall now includes `pending_inbound_messages`, so coordination clients can see when a session already has unacknowledged inbound work queued.
- Oikos now has direct session-kernel coordination tools using the canonical names `peers`, `get_session_events`, `session_tail`, `message_session`, `check_messages`, and `ack_message`.
- API routes and Oikos coordination tools now share `session_coordination.py` for wall/tail/message serialization and inbox acknowledgement semantics, reducing contract drift without a broad router rewrite.
- Related active tasks:
  - `docs/specs/launch-runtime-simplification.md`
  - `docs/specs/oikos-proactive-operator.md`
