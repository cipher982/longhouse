# Managed Local Launch Response Contract

Status: Proposed hardening
Owner: Runtime Host managed launch + provider CLI wrappers
Created: 2026-06-22
Related:
- `docs/specs/session-identity-kernel.md`
- `docs/specs/managed-provider-session-contract.md`
- `docs/specs/agents-machine-surface.md`

## Why This Exists

`longhouse claude` briefly regressed after the session identity kernel stopped
projecting synthetic provider ids. The managed-local launch route still created
a managed Claude session, but its response omitted the provider-native session
id needed to start the local Claude process:

```text
provider_session_id = null
managed_transport = claude_channel_bridge
```

The CLI correctly refused to attach because a Longhouse product session id is
not enough to launch or resume a provider process. CI missed the issue because
the launch-route test asserted "managed transport exists" but did not assert
the provider-specific response fields required by the CLI handoff.

This spec tightens the boundary between the Runtime Host and local provider
wrappers so a managed session cannot be reported as launchable when its response
is missing transport-required identity or attach data.

## Principles

- **Launch responses are executable contracts.** A successful managed-local
  launch response must contain every field the local wrapper needs for its next
  control step.
- **Provider ids are provider evidence.** `session.id` is a Longhouse product
  id, not a provider-native id. Any caller that needs provider control must
  write or observe a real provider-session alias first.
- **Validate by transport, not by provider folklore.** The response validator
  should key off `managed_transport` / control-plane semantics because that is
  what determines the attach/send mechanism.
- **Keep provider differences explicit.** Claude, Codex, OpenCode, and
  Antigravity have different local control mechanics. Shared helpers should
  encode those differences, not hide them behind a generic "managed" boolean.
- **No hidden fallbacks.** A missing required launch field is a server contract
  bug, not a reason to silently switch to unmanaged or observe-only launch.

## Current Shape

The route response is built in
`server/zerg/services/session_chat_impl.py::_managed_local_launch_response`.
It already validates:

- kernel capabilities grant live control or host reattach
- kernel capabilities include a managed transport

It does not validate transport-specific launch requirements such as:

- Claude channel bridge requires a non-synthetic `provider_session_id`
- Claude channel bridge attach command must target that provider id
- transports without an attach command should make that absence explicit in the
  test contract

The managed-local launcher currently writes session, thread, run, and
connection rows in
`server/zerg/services/managed_local_launcher.py::launch_managed_local_session_sync`.
Claude now preallocates a provider session id and records it as a
`provider_session_id` thread alias before the attach command is built.

## Target Contract Matrix

| Transport | Launch response requirements | Why |
| --- | --- | --- |
| `claude_channel_bridge` | `provider_session_id` present, non-synthetic, and present in `attach_command`; `attach_command` exports `LONGHOUSE_PROVIDER_SESSION_ID` | Local Claude is started with `claude --session-id <provider_session_id>` and channel env must bind provider and Longhouse ids separately. |
| `codex_app_server` | `provider_session_id` may be absent; `attach_command` invokes `longhouse-engine codex-bridge attach --session-id <longhouse_session_id>` | Codex bridge state is keyed by Longhouse session id; provider thread id is discovered by the bridge/app-server after launch. |
| `opencode_server_bridge` | `provider_session_id` may be absent at row birth; `attach_command` invokes `longhouse opencode-channel attach --session-id <longhouse_session_id>` | OpenCode server bridge creates/discovers provider session state in the local bridge path. |
| `antigravity_hook_inbox` | `provider_session_id` may be absent; `attach_command` empty | Antigravity launch is managed observe/send via hook inbox and does not advertise reattach or interrupt. |

`antigravity_process` exists as a legacy/direct-process transport enum, but it
is not the current managed-local launch contract. `longhouse agy` should produce
`antigravity_hook_inbox`; if `antigravity_process` appears in this response
path, the validator should fail until the launch contract is deliberately
redefined.

If a new transport is added, this table must gain a row in the same change as
the transport registration.

## Planned Changes

### 1. Centralize Launch Response Validation

Add a small helper near `_managed_local_launch_response`, for example:

```python
def _validate_managed_local_launch_response_contract(
    *,
    session: AgentSession,
    response: ManagedLocalSessionLaunchResponse,
) -> None:
    ...
```

It should run after the response object is constructed and before returning to
the router. It should raise `RuntimeError` for impossible server-produced
responses. This keeps bugs loud in tests and logs without expanding the public
API surface.

Initial checks:

- all responses: `session_id`, `provider`, `managed_transport`, and
  `source_runner_name` are non-empty
- `claude_channel_bridge`: `provider_session_id` is present and differs from
  `session_id`; `attach_command` contains the provider id and
  `LONGHOUSE_PROVIDER_SESSION_ID` with case-sensitive checks
- `antigravity_hook_inbox`: `attach_command` is empty
- `codex_app_server`: attach command names `codex-bridge attach` and the
  Longhouse session id
- `opencode_server_bridge`: attach command names `opencode-channel attach` and
  the Longhouse session id

The helper should avoid parsing shell in depth. Case-sensitive substring checks
are sufficient here because lower-level command builders already own shell
quoting tests.

### 2. Make Initial Provider Identity Explicit

Replace inline provider-id allocation in `launch_managed_local_session_sync`
with a helper:

```python
def _initial_provider_session_id_for_spawn(provider: str) -> str | None:
    if provider == "claude":
        return str(uuid4())
    return None
```

This is deliberately small. It documents that Claude's native channel launch
requires a predeclared provider id while Codex/OpenCode/Antigravity do not use
that local birth path.

### 3. Strengthen Projection Comment

Update `is_synthetic_provider_session_id` / `project_provider_session_id` with
one explicit sentence:

> A Longhouse session id is never enough to launch or resume a provider process;
> write a provider-session alias first when a control path needs one.

This keeps the identity-kernel invariant visible at the projection site that
caused the regression.

### 4. Add Transport Matrix Tests

Extend `server/tests_lite/test_managed_local_launch.py` with a compact
transport contract test matrix over current managed-local providers.

The existing Claude launch test should remain as the high-signal regression
test. The new matrix should assert each provider's expected response shape so
future projection or launcher cleanup cannot make a response "managed but not
attachable" again.

Suggested cases:

- Claude: non-synthetic provider id, attach command uses provider id, transport
  is `claude_channel_bridge`
- Codex: attach command uses `codex-bridge attach --session-id <session_id>`
- OpenCode: attach command uses `opencode-channel attach --session-id
  <session_id>`
- Antigravity: attach command empty, transport `antigravity_hook_inbox`

## Acceptance Criteria

- Managed-local launch responses are validated centrally before returning.
- Claude launch response cannot succeed with missing or synthetic
  `provider_session_id`.
- Existing provider attach semantics remain unchanged.
- A transport response matrix test covers Claude, Codex, OpenCode, and
  Antigravity.
- The managed-local launch route has explicit OpenCode response-shape coverage.
- The session-kernel projection comment explains synthetic provider ids in
  launch/control terms.

## Test Plan

Run the focused backend tests first:

```bash
cd server && ./run_backend_tests_lite.sh tests_lite/test_managed_local_launch.py tests_lite/test_managed_local_transport.py tests_lite/test_session_kernel_projection.py
```

Then run the supported backend tier:

```bash
make test
```

If only docs/tests/Python server helpers changed, `make test-ci` is optional
before push but recommended because this boundary previously escaped CI.

## Non-Goals

- Do not change provider CLI invocation semantics.
- Do not alter machine-agent heartbeat lease behavior.
- Do not introduce a generic launch-contract framework.
- Do not backfill historical sessions.
- Do not restore synthetic provider ids as a compatibility fallback.
