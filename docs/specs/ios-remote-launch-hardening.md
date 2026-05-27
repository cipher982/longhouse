# iOS Remote Launch Hardening

Status: Active prelaunch buildout
Owner: mobile + managed session control
Last updated: 2026-05-27

## Goal

Make "Start session" from iOS feel like a first-class Longhouse launch path:
pick a machine, pick a real workspace from recent/common choices, see the
provider being launched, start a managed Codex session, and send the first
prompt without falling through a legacy control path.
Codex is no longer the only launchable provider; iOS should render the same
provider choices the Machine Agent advertises for Codex, Claude, and OpenCode.

This is prelaunch. Prefer correcting the contract now over preserving
compatibility with half-migrated launch state.

## Current Findings

- iOS remote launch must not be Codex-only in UI logic; provider choices come
  from `launchable_providers`.
- iOS still requires manual absolute cwd entry. Web already derives recent cwd
  suggestions from timeline sessions for the selected machine.
- iOS navigates after launch without checking `launch_failed` or
  `launch_orphaned`.
- iOS maps any 502 to "Generation failed", which hides useful managed-control
  errors such as "Session is not managed_local".
- Remote launch was partially migrated to the session identity kernel:
  `SessionLaunchAttempt`, `SessionRun`, and `SessionConnection` are created,
  but some response/debug code still writes or reads deleted `AgentSession`
  `launch_*` columns through transient legacy shims.
- Live send still rejects remote-launched sessions through legacy
  `AgentSession.execution_home` checks even when the kernel capability
  projection shows a live `codex_bridge` connection.

## Product Shape

The iOS launch sheet should expose exactly what Longhouse is doing:

```text
Start Session

Machine
  cinder                         online

Provider
  Codex                          launchable on cinder
  Claude                         launchable on cinder
  OpenCode                       launchable on cinder

Workspace
  ~/git/zerg/longhouse           recent
  ~/git/zerg                     recent
  ~/git/me                       recent
  Other path...

Display name
  optional

Start
```

Rules:

- Machine remains first because placement is the execution owner.
- Provider is visible and selectable for each entry in `launchable_providers`.
- Provider choices are driven by `supports[]` (`<provider>.launch`), not
  hardcoded UI assumptions.
- Recent workspaces are scoped to `(owner, device_id)`.
- Manual absolute path remains available, but it is no longer the primary
  path.
- Launch success navigates only for `live` and `launching_unknown`.
- Launch failure stays in the sheet with typed, user-actionable copy.

## Backend Contract

Remote launch is kernel-native:

1. `POST /api/sessions/launch` validates user, target device, provider support,
   and absolute cwd.
2. Runtime Host pre-allocates the session and creates:
   - primary `SessionThread`
   - `SessionLaunchAttempt(state=pending)`
3. Runtime Host dispatches `session.launch` to the Machine Agent.
4. On success it records:
   - `SessionRun(launch_origin=longhouse_spawned)`
   - `SessionConnection(control_plane=codex_bridge, state=attached,
     can_send_input=1, ...)`
   - `SessionLaunchAttempt(state=dispatched, run_id=...)`
5. On typed engine failure it records:
   - `SessionLaunchAttempt(state=failed, error_code, error_message)`
   - no live run/connection
6. On control-channel timeout it records:
   - `SessionLaunchAttempt(state=dispatched, expires_at still set)`
   - response state `launching_unknown`

`AgentSession.launch_*` must not be the source of truth. They may remain
read-only compatibility projections until deleted, but launch lifecycle reads
must derive from `SessionLaunchAttempt`.

Live send/interrupt/steer must use the same kernel projection that drives
`session.capabilities`. A session with an active `SessionConnection` granting
`can_send_input=1` and `control_plane=codex_bridge` is managed and sendable
even if legacy `AgentSession.execution_home` is empty or stale.

## API/UI Contract

Machine directory remains the launchability source:

- `GET /api/timeline/machines`
- `GET /api/agents/machines`

The response should grow toward provider-generic launch readiness, but this
phase can keep `can_launch_codex` while iOS derives a visible provider row from
`supports[]`.

Workspace suggestions can initially reuse timeline session listing, matching
the web implementation:

- fetch sessions filtered by selected `device_id`
- collect session/head/root `cwd`
- include useful parent paths
- dedupe absolute paths
- prefer most recent

A future endpoint can make this cleaner:

- `GET /api/timeline/machines/{device_id}/workspaces`
- `GET /api/agents/machines/{device_id}/workspaces`

Do not block this phase on that endpoint if the existing session list gives a
good enough iOS experience.

## Error Handling

Backend errors should be structured when the client can act on them:

```json
{
  "detail": {
    "error_code": "send_failed",
    "message": "Session is not managed by the current control path."
  }
}
```

iOS should parse both existing structured shapes:

- `{"detail": {"error_code": "...", "message": "..."}}`
- `{"detail": {"code": "...", "message": "..."}}`

Generic HTTP 502 should not become "Generation failed" for managed-control
send. Use route-specific fallback copy such as "Send failed. The control path
did not accept the message."

## Implementation Phases

### Phase 1: Backend Launch/Send Contract

- Derive launch response/debug state from `SessionLaunchAttempt`.
- Make idempotency lookup use `SessionLaunchAttempt.client_request_id` instead
  of transient `AgentSession.launch_client_request_id`.
- Rework remote-launch tests around kernel tables and unskip them.
- Make managed-control send accept kernel-managed Codex sessions.
- Return structured send errors from `/api/sessions/{id}/input`.

### Phase 2: iOS Launch UX

- Add provider model/row to the launch sheet.
- Port recent workspace suggestions from web.
- Auto-fill the most recent workspace for the selected machine.
- Add tap-to-select workspace rows/chips and keep manual path entry.
- Handle `launch_failed` and `launch_orphaned` in the sheet.
- Preserve structured launch/send errors.

### Phase 3: Verification

- Backend: focused pytest for remote launch, machine directory, session input,
  managed local transport/control.
- iOS: Swift tests for API error parsing and launch sheet view model/helper
  logic where practical.
- Live e2e: launch from iOS-compatible API path on hosted `david010`, send a
  first prompt, verify hosted DB/API show:
  - launch attempt dispatched
  - run + attached connection
  - input delivered
  - turn accepted/durable or active
  - no generic 502

## Acceptance Criteria

- iOS no longer asks for an absolute path as the default path.
- iOS clearly shows the provider being launched.
- A remote-launched Codex session can receive the first iOS prompt.
- Remote launch state survives process/database reload because it lives in
  kernel tables, not transient ORM shims.
- Failed launch/send cases show the real reason.
- The old skipped remote-launch test module is replaced with active tests.
