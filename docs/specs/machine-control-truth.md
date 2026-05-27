# Machine Control Truth

Status: Active epic
Owner: Machine Agent control path + launch UX
Updated: 2026-05-13

## Goal

Make remote launch boring to dogfood.

A user should be able to open Longhouse and answer four questions without
reading logs:

- Which of my machines are reachable for live control?
- What live-control operations can each machine do?
- Why is a machine unavailable?
- Can I start Codex there now?

This epic hardens the Codex remote-launch v1 that already exists. It does not
move durable transcript ingest onto WebSockets. Longhouse keeps the existing
split:

- durable data plane: Machine Agent HTTPS POST with local spool/retry
- UI wake plane: Runtime Host SSE/EventSource after committed state changes
- control plane: Machine Agent outbound WebSocket for typed commands

## Non-Goals

- No durable transcript/event ingest over the control WebSocket.
- No generic data replication bus over the control WebSocket.
- No Runner dependency for the core launch/control path.
- No generic remote shell.
- No offline launch queue.
- No session migration between machines.
- No workspace sync, rsync, or git automation.
- No provider binary management.
- No provider launch without a provider-specific managed control path.
- No machine-management dashboard in this epic.
- No metrics dashboard or generalized SLA platform.

## Constraints

- Machine Agent is authoritative for control reachability, supported commands,
  provider launch, and cwd validation.
- Runtime Host may mirror machine truth, but must not invent launch capability.
- Browser, iOS, and CLI consume the same machine truth fields.
- SQLite remains the core Runtime Host database.
- Keep health axes separate:
  - shipping health
  - live UI wake health
  - control-channel reachability
  - provider launch readiness
- cwd policy is not machine health. cwd is validated per launch attempt.

## Truth Model

Machine-level control status should be primitive, not a large product-state
enum:

```json
{
  "control_channel_status": "connected",
  "supports": ["codex.send", "codex.interrupt", "codex.steer", "codex.launch", "codex.continue"],
  "can_launch_codex": true,
  "launch_blocked_by": null
}
```

When launch is unavailable:

```json
{
  "control_channel_status": "disconnected",
  "supports": [],
  "control_operations_by_provider": {},
  "can_launch_codex": false,
  "launch_blocked_by": "control_down"
}
```

Known `launch_blocked_by` values:

- `control_down` — no active Machine Agent control WebSocket
- `no_launch_support` — connected engine did not advertise any remote-launch
  provider capability
- `no_codex_support` — legacy value for connected engines that did not
  advertise `codex.launch`
- `engine_too_old` — reserved for minimum-build gating if we need it
- `auth_failed` — reserved for local-health/control diagnostics
- `runtime_unreachable` — reserved for local-health/control diagnostics

The launch sheet should render `launch_blocked_by`. It should not rederive
capability logic from raw fields.

`supports[]` remains the raw Machine Agent hello frame. Consumers that need a
provider-level read model should use `control_operations_by_provider`, for
example `{"antigravity": ["send"]}` for a machine that can inject
Antigravity hook-inbox input but cannot remote-launch Antigravity.

## Task List

### Epic A — Machine Control Truth

1. Add derived launch-readiness fields to the shared machine directory
   response:
   - `control_channel_status`
   - `can_launch_codex`
   - `launch_blocked_by`
2. Update web launch UX to consume those derived fields.
3. Keep stale/offline machines from dominating the launch modal.
4. Extend local-health / engine status with control-channel primitives:
   - configured/enabled/connected
   - runtime or WS target
   - last connected/disconnected
   - last error code/message
   - advertised `supports[]`
   - derived `control_operations_by_provider`
   - engine build
5. Make `make dogfood-check` print the same readiness reason the hosted launch
   sheet shows.
6. Mirror the local truth through `/api/agents/machines` and
   `/api/timeline/machines` without a parallel backend truth table.

### Epic B — Launch UX On Top Of Truth

1. Gate Start on `can_launch_codex`.
2. Render unavailable states from `launch_blocked_by`.
3. Keep launch lifecycle states visible:
   - `launching`
   - `live`
   - `launching_unknown`
   - `launch_failed`
   - `launch_orphaned`
4. Never render launch-in-progress or launch-failed sessions as normal empty
   transcripts.
5. Preserve typed Machine Agent launch errors in session detail.

## Success Criteria

- After `make dogfood-refresh` on `cinder`, local health shows the control
  channel connected and `codex.launch` supported.
- `make dogfood-check` prints a concrete launch-readiness reason that matches
  the hosted launch sheet field names.
- `/api/timeline/machines` shows `cinder` online and `can_launch_codex=true`
  shortly after the Machine Agent connects.
- Stale QA/stress/offline devices do not flood the launch modal.
- If TLS, auth, runtime URL, or control WebSocket setup breaks, local health
  and the launch sheet identify the failure class instead of only saying that
  `codex.launch` is missing.
- A web launch into `/Users/davidrose/git/zerg/longhouse` creates a managed
  Codex session on `cinder`, reaches `live` or a typed actionable failure, and
  session detail can send the first turn.
- Tests cover backend machine truth, frontend launch gating/empty states,
  engine control status reporting, and launch lifecycle edge cases.
