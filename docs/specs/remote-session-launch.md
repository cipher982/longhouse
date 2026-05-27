# Remote Session Launch

Status: Implemented in `remote-session-launch`; Codex, Claude, and OpenCode are launchable when the target Machine Agent advertises provider-specific support. Directory picker, presets, Antigravity launch, and richer SLA telemetry remain deferred.
Owner: machine control + mobile/web launch UX
Updated: 2026-05-27

## Goal

Let a user start a new managed session on one of their own already-enrolled
machines from iOS or web, without first opening a terminal on that machine.

Managed sessions can be created by `longhouse codex`, `longhouse claude`, or
`longhouse opencode` running on the target machine itself. That machine POSTs to
`/api/sessions/managed-local/this-device` with its own device token.

After this spec, a user on iPhone or browser picks a machine, picks a
workspace (cwd), picks a provider, and says "start it there." The target
Machine Agent receives a typed `session.launch` command over its existing
control WebSocket, spawns the provider locally through the provider's managed
transport, and reports the pre-allocated session id back.

This is a natural extension of `machine-agent-control-channel.md`: Phase 2
gave us `session.send_text` / `interrupt` / `steer_text` on known sessions.
This spec adds `session.launch` — a command that happens to create the
session rather than act on an existing one.

## Non-Goals

- No session migration between machines. Sessions run where launched.
- No workspace filesystem sync, rsync, or git auto-commit on launch.
- No Longhouse-provisioned runtime. Machines are user-owned.
- No Antigravity remote launch until launch/reattach semantics are proven
  beyond the send-only hook inbox.
- No "queue launch until machine comes online." Machine must be online at
  request time. Queueing is a jobs product.
- No generic remote shell. `session.launch` spawns one Longhouse-managed
  provider session, nothing else.
- No continuation of a prior session into a fresh launch in v1. Fresh
  sessions only.

## Release Scope

The current release scope:

- machine directory endpoints for browser and machine clients
- `POST /api/sessions/launch`
- `session.launch` over the Machine Agent control WebSocket
- detached-UI Codex bridge startup with thread creation
- PTY-backed Claude channel launch
- OpenCode server-bridge launch with `opencode attach` reattach
- web and iOS launch sheets
- launch lifecycle fields on `sessions`
- launch reaper and admin debug endpoint

Directory picker, workspace presets, Antigravity launch, queued offline launch,
and propagation SLA dashboard integration are intentionally deferred.

## Product Shape

### Launch sheet — cold path first

A new user has no sessions. The UX leads with **machines** (what they
just enrolled) and a typed cwd on the target Machine Agent (which is
authoritative for cwd validation). A directory picker and presets are
additive follow-ups.

```
Start Session
──────────────
Machine
  [cinder]  online     · Codex ✓  Claude ✓  OpenCode ✓
  [homelab] offline              (disabled)

Workspace on cinder
  /Users/david/git/zerg

Provider     [Codex | Claude | OpenCode]

                                                [Start]
```

- Machine list is first — it is what new users actually have.
- Workspace is a typed absolute cwd in v1. A directory picker backed by
  the Machine Agent and recent workspaces are deferred accelerators.
- Provider choices come from live `supports[]` values such as `codex.launch`,
  `claude.launch`, and `opencode.launch`.
- Initial prompt is deferred from the Codex v1 release. The v1 launch
  creates a steerable empty session; the user sends the first prompt from
  session detail.

### After "Start"

- Success deep-links into session detail, which renders the provider's managed
  control capabilities from the kernel projection.
- Codex runs detached-UI under the engine-owned app-server bridge. Claude runs
  through the channel launch path. OpenCode runs through the localhost server
  bridge. None of these are one-shot prompt-and-exit execution.
- A user at a terminal on the target machine can later reattach through the
  provider-specific attach command.

## First-Principles Invariants

1. **One session, one execution owner.**
   The target Machine Agent is the execution owner from the moment the
   launch succeeds. Runtime Host coordinates; it does not execute.

2. **Workspace is user intent; machine is placement. Machine picks first.**
   New users have no workspace history. Leading with machines matches
   reality. Recents are an accelerator, not the bootstrap.

3. **No hidden state transfer.**
   Launch carries only declared parameters: cwd, provider, and optional
   repo context. No silent copy of files, secrets, or env from the
   requester's device.

4. **Online-only placement.**
   Target machine must have an active control-channel connection at the
   moment of request. Offline machines fail fast with clear copy.

5. **Pre-allocated session id. One launch attempt row.**
   Runtime Host mints the session UUID, inserts the `sessions` row, records a
   `SessionLaunchAttempt(state=pending)`, and includes `session_id` in the
   `session.launch` frame. The control-channel envelope rule - every command
   carries `session_id` - is preserved. Lifecycle is projected from the durable
   attempt row, not from legacy `AgentSession.launch_*` shims.

6. **Explicit per-provider per-op capability.**
   Only providers the Machine Agent announces in `supports[]` as
   `<provider>.launch` are offerable.

7. **Machine Agent is authoritative for cwd.**
   Runtime Host does not validate filesystem paths. The Machine Agent
   validates cwd exists, is a directory, and is allowed by local policy.

8. **cwd policy is enforced locally.**
   `send_text` is bounded by the session's existing cwd. `launch` picks
   cwd, so it widens the implicit code-running surface. Machine Agent
   validates that cwd is absolute and exists as a directory. Rejections
   return `cwd_not_allowed` for relative paths and `cwd_not_found` for
   missing paths. The UI should make recent cwd choices available so users
   rarely type full paths.

## Request Flow

```
iOS / Web (user auth cookie)
        │
        ▼
POST /api/sessions/launch
  - verify user owns target device_id
  - verify control channel online for device_id
  - verify provider ∈ device supports[] as <provider>.launch
  - mint session UUID
  - INSERT sessions row:
      owner_id, device_id, cwd, provider, display_name,
      git_repo, git_branch, project, started_at=now()
  - INSERT session_launch_attempts row:
      state=pending, command_id, expires_at, client_request_id
  - send session.launch command frame over control WS
  - await command_result (short timeout, 20s)
        │
        ▼
Machine Agent (WS)
  - check cwd exists + allowed by local policy
  - call cmd_codex_bridge_start(session_id, cwd, …)
    (engine/src/codex_bridge.rs:484 — existing seam)
  - wait for bridge status=ready
  - return command_result { ok=true, provider_session_id, transport }
        │
        ▼
Runtime Host
  - create run/connection rows
  - UPDATE session_launch_attempts row: state=adopted, run_id
  - return { session_id, launch_state } to client
        │
        ▼
Client deep-links to /sessions/{session_id}
```

### Timeout / mid-flight disconnect

- Runtime Host marks the attempt `state=dispatched`, projects
  `launch_state=launching_unknown`, and returns the session_id with that state
  to the client.
- Client polls `GET /api/sessions/{id}` (or subscribes to the existing
  session stream) for resolution.
- On Machine Agent reconnect, it sends any buffered late
  `command_result` frames (same LRU behavior as other control commands).
- If no result arrives before `expires_at` (e.g. 120s after request), Runtime
  Host moves the attempt to `state=abandoned`, projected as
  `launch_state=launch_orphaned`. No retry. The attempt becomes a cold record;
  timeline filters hide `launch_orphaned` from default views but leave it for
  debug.

### Failure

- Typed errors from Machine Agent (`cwd_not_found`, `cwd_not_allowed`,
  `provider_unsupported`, `provider_launch_failed`,
  `already_running_for_cwd`) propagate to the client and mark the attempt
  `state=failed`, projected as `launch_state=launch_failed` with
  `launch_error_code` / `launch_error_message`.

## Data Model

### `session_launch_attempts`

```text
session_id            uuid not null
thread_id             uuid nullable
run_id                uuid nullable
provider              text not null
host_id               text nullable
owner_id              integer nullable
client_request_id     text nullable
command_id            text nullable
state                 text not null  -- pending | dispatched | adopted
                                      -- | failed | abandoned
error_code            text nullable
error_message         text nullable
expires_at            timestamptz nullable
```

`project_remote_launch_lifecycle()` maps attempt rows to the public states:
`launching`, `live`, `launching_unknown`, `launch_failed`, and
`launch_orphaned`. Sessions with no attempt are not remote-launch sessions and
surface `launch_state=null`.

### Workspace presets (derived, not stored)

Presets on the launch sheet are a read-only projection — same as before,
but explicitly secondary to the machine picker:

```sql
SELECT cwd, provider,
       max(git_repo)   AS git_repo,
       max(git_branch) AS git_branch,   -- NOTE: lossy; see Open Q 4
       max(project)    AS project,
       max(created_at) AS last_active_at,
       count(*)        AS session_count
FROM sessions
WHERE owner_id = ? AND device_id = ?
GROUP BY cwd, provider
ORDER BY last_active_at DESC
LIMIT 10;
```

Presets are scoped per-(owner, device). A session on laptop-A does not
surface as a preset on laptop-B — that implies filesystem knowledge we
don't have.

## Endpoints

### Machines (Phase 0 — ships now)

Follow the agents-sessions pattern: one builder, two route wrappers.

- `GET /api/agents/machines` — machine-token auth
- `GET /api/timeline/machines` — user-cookie auth

Both return:

```json
[
  {
    "device_id": "cinder-abc123",
    "machine_name": "cinder",
    "online": true,
    "supports": ["codex.send", "codex.interrupt", "codex.steer", "codex.launch", "codex.continue"],
    "last_seen_at": "2026-05-12T13:44:22Z",
    "engine_build": "29db1495"
  }
]
```

The list reads from the control-channel in-memory registry for
online/supports/last_seen, joined with persisted device metadata for
machines that are currently offline but have been seen before.

### Launch (Phase 1)

- `POST /api/sessions/launch`
  body:
  ```json
  {
    "device_id": "cinder-abc123",
    "provider": "codex",
    "cwd": "/Users/david/git/zerg",
    "git_repo": "zerg",
    "git_branch": "main",
    "project": "zerg",
    "display_name": null
  }
  ```
  returns:
  ```json
  {
    "session_id": "…",
    "launch_state": "live",
    "launch_error_code": null,
    "launch_error_message": null
  }
  ```

Session state transitions post-response are observable via the existing
`GET /api/sessions/{id}` and session stream. No separate launch-request
endpoint exists because there is no separate launch-request resource.

### Control channel (Phase 1)

`session.launch` is a new command type on the existing control WebSocket.
The frame carries `session_id` like every other command:

```json
{
  "type": "command",
  "command_id": "…",
  "session_id": "pre-allocated-uuid",
  "command_type": "session.launch",
  "payload": {
    "provider": "codex",
    "cwd": "/Users/david/git/zerg",
    "git_repo": "zerg",
    "git_branch": "main",
    "project": "zerg",
    "display_name": null,
    "owner_id": "…"
  }
}
```

Response:

```json
{
  "type": "command_result",
  "command_id": "…",
  "ok": true,
  "result": {
    "session_id": "…",
    "provider_session_id": "…",
    "transport": "codex_app_server"
  }
}
```

Failure codes (mapped to `launch_error_code`): `cwd_not_found`,
`cwd_not_allowed`, `provider_unsupported`, `provider_launch_failed`,
`launch_timeout`, `already_running_for_cwd`.

## Authorization

- `POST /api/sessions/launch` requires a user auth cookie.
- Runtime Host verifies:
  - `device_id` belongs to this user (via device registration ownership)
  - target is the user's — no cross-account launch
- Initial prompt is deferred from v1. Launch permission creates the
  managed session; the follow-up send uses the existing live-session
  permission path.

**Why no 2FA in v1**: Device enrollment already granted code-running
trust. The existing `send_text` primitive can already steer an LLM that
can write and run code. Launch widens the implicit surface by letting the
caller pick cwd — the `cwd_allowlist` (Invariant 8) bounds that widening.
If, post-launch, we observe abuse or user demand for stronger guarantees,
add a machine-local "confirm on device" hook. Not required for v1.

## Engine Reuse

Confirmed reusable without duplication:

- Validate cwd + policy in a new small Rust helper
  (`engine/src/launch_policy.rs`).
- Call `cmd_codex_bridge_start` in `engine/src/codex_bridge.rs:484`
  directly — the existing Rust seam spawns the detached `codex-bridge
  run` daemon, waits for `ready`, writes state files.
- Return success with `session_id`, `provider_session_id`, `transport`.

**Explicitly not reused**: `_run_native_codex_tui` and
`_run_foreground_process_group` in `server/zerg/cli/codex.py`. These
attach a terminal. Remote launch is detached-UI managed: the engine handler
must not attach a TUI, but it must leave a long-running steerable bridge and
app-server session rather than a one-shot prompt-and-exit process.

Device identity: the handler reads `device_id` from the engine's own
config. Runtime Host does not pass it in the frame. This differs from
`/managed-local/this-device` which derives it from the caller's token;
the caller here is user-authed, so the target must be named explicitly
upstream (in the POST body) and routed by the control-channel
registry.

## Capability Gating

Launch button enabled iff:

- target `machine.online = true`
- `<provider>.launch ∈ machine.supports`

Offline / unsupported states reuse the existing control-capability copy
patterns. No new design language.

## Phased Plan

### Phase 0 — machines endpoints (ships now)

- `GET /api/agents/machines` (machine-token)
- `GET /api/timeline/machines` (user-cookie)
- Shared builder joining the in-memory control-channel registry with
  persisted device metadata.
- Backend tests: empty list, online/offline mix, supports[] echo, parity
  between routes, user-scoped filtering.

Acceptance:
- Both routes return the same body for the same user.
- Offline-but-seen machines appear with `online=false, supports=[]` (last
  known supports are not persisted — avoid implying stale truth).
- Unknown users return `[]` (not 404).

### Phase 1 — `session.launch` + `SessionLaunchAttempt` lifecycle

- DB migration: add `session_launch_attempts`.
- `POST /api/sessions/launch` endpoint.
- Control-channel `session.launch` command dispatch.
- Engine handler in `control_channel.rs` validates cwd, dispatches to the
  provider-specific launch adapter, and returns the typed result.
- Engine `supports[]` includes only launch providers whose stock binaries are
  present on PATH.
- `launch_error_code` + `launch_error_message` surface on
  `GET /api/sessions/{id}` for clients to render.

Acceptance:
- Launch happy-path creates a session plus attempt projected as
  `launch_state=live`.
- Bogus cwd returns `cwd_not_found`, attempt ends in `launch_failed`.
- cwd outside policy returns `cwd_not_allowed`, attempt ends in
  `launch_failed`.
- Offline device returns 409 `machine_offline` without inserting a row.
- Rapid double-submit returns one `live` row + one `already_running_for_cwd`
  via engine LRU.
- Command-result arriving after Runtime Host timeout moves the attempt from
  `launching_unknown` to `live` or `launch_failed` deterministically.
- Sessions with no `SessionLaunchAttempt` surface `launch_state=null`.

### Phase 2 — Web launch sheet

- Launch sheet UI using `/timeline/machines` + typed cwd.
- Machine-first ordering in the UI.
- Presets surface under the selected machine once history exists.
- Deep-link on success.

Acceptance:
- Zero-history user can complete a launch by entering a cwd.
- Second-session user can reuse a cwd manually.
- Offline machine visually disabled with clear copy.
- E2E test covers happy-path launch from click to transcript render.

### Phase 3 — iOS launch sheet

- Mirror web UX.
- Existing user-cookie auth (no machine token).
- Xcode UI test covers happy-path launch.

### Phase 4 — telemetry/admin debug

- Hidden admin view of launch attempts filtered by projected `launch_state !=
  live` for debugging.
- Propagation metric: POST-to-first-transcript-byte for remote launches,
  tracked alongside existing managed-op SLAs. Deferred.

## Directory Picker (Phase 2/3 sub-spec)

The Machine Agent serves directory listings over a new narrow control
command `machine.list_dir`:

```json
{
  "command_type": "machine.list_dir",
  "payload": { "path": "/Users/david/git" }
}
```

Response returns entries that are directories only, with an `is_git`
boolean and `last_session_at` (if any). Entries outside policy are
omitted, not merely flagged — the picker cannot surface a path the
launch would reject.

The picker is scoped: browsing is rooted at `$HOME` by default. A user
who wants a different root edits machine policy config. Out of scope for
v1 UI.

## Testing Plan

Backend (Phase 0):
- machines routes: empty, mixed, parity
- supports[] reflection
- offline-but-persisted surfaces with `online=false, supports=[]`

Backend (Phase 1):
- `POST /api/sessions/launch` happy path
- pre-allocated UUID persists in row before command is sent
- timeout path: row moves to `launching_unknown`, then resolves
- `launch_orphaned` after lease expiry without result
- offline device → 409, no row
- authorization: user cannot launch on a device_id they don't own

Engine (Phase 1):
- `session.launch` handler validates cwd
- policy rejection returns `cwd_not_allowed`
- happy path calls `cmd_codex_bridge_start` and returns the typed result
- duplicate `command_id` dedupe via existing LRU
- `already_running_for_cwd` when the local bridge registry shows an
  active bridge for the same `(cwd, provider)`
- `supports[]` includes `codex.launch` iff bridge seam is available

End-to-end (Phase 2/3):
- web: pick machine → pick cwd → launch → transcript renders
- iOS: same, fixture-backed
- offline machine path: disabled UI; direct POST returns 409

## Open Questions

1. Directory picker scope. v1 defaults to `$HOME`. Do we want a
   machine-agent-side "favorites" (bookmarks of common roots) from day
   one, or defer until users ask?

2. Initial prompt. Deferred from Codex v1. When added, prefer sending it
   as a follow-up `session.send_text` after `launch` succeeds so launch
   semantics stay narrow.

3. Rate limiting. `already_running_for_cwd` is cheap on the engine.
   Do we need a Runtime Host rate limit, or is engine-side idempotency
   sufficient for launch?

4. Preset branch aggregation. The projection uses `max(git_branch)` for
   grouping by `(cwd, provider)`. Same cwd with multiple branches
   collapses the branch silently. Options: (a) group by
   `(cwd, provider, git_branch)` and let presets expand; (b) drop branch
   from preset entirely and let the user confirm branch post-launch;
   (c) keep current lossy aggregation and ship. Recommendation for v1:
   (b) — branch is a session concern, not a workspace identity concern.

5. Cross-machine presets. If I always launch in `~/git/zerg` on laptop,
   should that surface as a hint when I switch to homelab? Out of scope
   for v1 — implies shared filesystem knowledge. Flag for post-launch.

## Deletion Targets

After Phase 1 ships:

- Nothing is deleted in v1. `/managed-local/this-device` stays as the
  `longhouse codex` CLI path.

After dogfooding Phase 1-3 for a month, consider:

- Collapsing `/managed-local/this-device` into `/sessions/launch` with
  machine-token auth, making them one endpoint. Out of scope for this
  spec.

## Review Provenance

This spec was revised after three independent reviews:

- Repo-aware subagent review — flagged `launch_requests` redundancy,
  confirmed `cmd_codex_bridge_start` as the clean Rust seam, pointed out
  the `device_id` plumbing bug in the old draft.
- Hatch Opus review — pushed for machines-first UX, explicit cwd
  allowlist, and the Phase 1 deferral gate.
- Hatch DeepSeek review — confirmed pre-allocated UUID as strictly
  better, agreed `session.launch` belongs on the control channel if the
  durable-FSM rule is respected.

All three agreed on: drop the parallel table, lead UX with machines, and
wire `cwd_not_allowed` as a real policy. The original staged plan shipped
Phase 0 first; the implementation branch now carries the full Codex v1.
