# Cursor Permission Policy

Status: implementation plan
Owner: Longhouse managed-provider surfaces
Date: 2026-07-20

## Outcome

Longhouse treats permission handling as an explicit launch policy, not an
implicit consequence of a managed session.

The human user is the only remote approver. Longhouse carries a pending
provider request to web or iOS and returns the user's Allow or Deny decision;
Longhouse and its agents do not approve tools autonomously.

Cursor exposes three product policies:

| Policy | Meaning |
| --- | --- |
| `auto_approve` | The provider runs without interactive permission pauses. Deterministic local guards such as DCG remain independent. |
| `provider_local` | The provider's native local permission behavior remains in the interactive TUI. Longhouse does not route approvals. |
| `remote_human` | The provider pauses and the human answers through Longhouse web or iOS. |

The defaults are surface-specific:

- Cursor Helm defaults to `provider_local`.
- Cursor Console defaults to `auto_approve`; `provider_local` is invalid because
  Console has no local TUI.
- Cursor Shadow has no Longhouse permission authority.

## Why This Changes

Cursor Helm currently defaults to the legacy `remote_approve` mode. Its global
hook routes every Shell and MCP invocation through Longhouse and denies after a
bounded wait. A live incident registered ten permission requests and received
successful HTTP responses for every decision poll, but no human answered. The
hook called that state "Longhouse approval unavailable" and denied each tool.

The permission transport worked. The product default and failure taxonomy were
wrong. The July 17 parity work correctly proved that remote Allow and Deny were
possible, then incorrectly promoted that capability into Helm's default policy.

## Surface Contract

### Helm

`longhouse cursor` launches with `provider_local`. It does not set
`LONGHOUSE_PERMISSION_HOOK_ENABLED`, `LONGHOUSE_HOOK_URL`, or
`LONGHOUSE_HOOK_TOKEN`, and it performs no permission API requests.

The user may explicitly select:

- `--permission-policy auto_approve`
- `--permission-policy provider_local`
- `--permission-policy remote_human`
- `--remote-approve` as a convenience alias for `remote_human`

The existing `--permission-mode` spelling remains accepted during migration.
For Cursor Helm only, legacy `bypass` means `provider_local`; legacy
`remote_approve` means `remote_human`. Compatibility is resolved using both the
provider and launch surface. `bypass` must not become a global alias for
`auto_approve`, because that would change historical Helm behavior.

Helm `auto_approve` must use a proven stock Cursor flag or configuration. If the
interactive provider exposes no reliable per-launch autonomous switch, reject
that policy with a typed CLI error rather than silently treating it as
`provider_local`.

When `remote_human` is selected, launch waits for a registered session-scoped
hook token and clearly prints that Shell and MCP calls pause for human approval
in Longhouse. The session URL and wait budget are visible.

Resume reuses the recorded policy from the local binding claim. An explicit
conflicting CLI policy fails with a clear error; resume never silently changes
permission authority. Claims created before policy recording are ambiguous
because historical default-remote and explicit-bypass launches used the same
claim shape. Those resumes warn and select `provider_local`; they never silently
activate remote Longhouse authority.

### Console

Cursor Console remains autonomous by default. Legacy Console `bypass` maps to
`auto_approve` and continues to use Cursor's stock autonomous flag.
`provider_local` is rejected because no local permission UI exists.

`remote_human` is advertised only after Console receives a session-scoped hook
token accepted by the permission API. A Machine Agent device token is not a
substitute. Until that path is proven, Console returns a typed
`permission_policy_unsupported` error for `remote_human`.

### Shadow And DCG

Shadow sessions never gain Longhouse permission authority. The user-level
Longhouse hook may observe lifecycle evidence, but the remote gate remains
dormant.

DCG is a separate local destructive-command guard. Longhouse installation
preserves its hook entry and order. DCG may deny in every local policy; selecting
a Longhouse policy never disables or replaces it.

## Remote-Human Failure Contract

Once `remote_human` is explicitly engaged, it is fail-closed:

- explicit Allow permits the one invocation;
- explicit Deny blocks it;
- no decision before the deadline blocks it;
- transport or authentication failure blocks it;
- malformed or unknown decisions block it;
- session, launch, or provider identity mismatch blocks it.

These outcomes have distinct user messages and diagnostic codes. A successful
registration followed by pending HTTP 200 polls is `timeout_no_decision`, not a
Longhouse outage.

Permission registration receives a bounded `expires_at`. When the local hook
deadline ends after registration, the hook best-effort resolves that exact
interaction as `expired`. Catalog reads also treat the deadline as terminal, so
web and iOS cannot answer a request after the provider has already denied it.
Polls after expiry return a terminal deny/expired result rather than pending.

Permission presentation is provider-specific: Cursor requests say Cursor, not
Claude. The underlying interaction remains the shared permission-prompt model.

## Hook Safety

Cursor hook configuration is file-based and `failClosed` is static per global
hook entry; it cannot vary by launch environment.

Longhouse therefore separates:

- lifecycle and transcript telemetry hooks, which remain fail-open; and
- a minimal permission hook path, which remains fail-closed.

The permission hook checks whether the remote gate is enabled before performing
filesystem or network work. In dormant mode it immediately emits `{}` and exits
successfully. This minimizes but cannot eliminate the provider limitation: a
Cursor hook-runner failure before the script starts can block Shell or MCP in
Shadow, `provider_local`, or `auto_approve` because the installed entry is
global. Longhouse must not claim per-run hook-failure isolation.

The current process-scoped environment is retained for session identity, URL,
and the scoped hook token. Launch clears inherited permission variables before
selectively enabling `remote_human`. A Unix-socket redesign is outside this
change.

## Implementation Plan

1. Add surface-aware Cursor policy normalization and Helm CLI options.
2. Persist the canonical Helm policy in binding claims and enforce it on resume.
3. Map canonical policies to existing wire values only at compatibility
   boundaries; preserve existing stored sessions.
4. Correct Console defaults and reject unsupported policies before provider
   spawn.
5. Split the minimal permission hook path from lifecycle telemetry.
6. Add typed timeout, transport/auth, malformed-result, and identity-mismatch
   outcomes.
7. Add interaction expiry plus best-effort hook abandonment using the existing
   terminal `expired` status.
8. Correct Cursor prompt copy and launch messaging.
9. Update the Cursor Helm parity specification to reference this policy.
10. Run targeted provider-boundary tests and the focused Cursor product canary.

## Acceptance Tests

- Bare Cursor Helm selects `provider_local`, exports no approval environment,
  and makes zero permission HTTP requests.
- Explicit Helm `remote_human` obtains a session-scoped hook token and proves
  Allow, Deny, no-decision timeout, transport failure, malformed response, and
  identity mismatch.
- A timed-out request is no longer answerable by ID in the active pause surface.
- Parent-shell permission variables cannot re-enable the gate under
  `provider_local` or `auto_approve`.
- Helm resume reuses the recorded policy and rejects an explicit conflict.
- Cursor Console defaults `auto_approve`, rejects `provider_local`, and does not
  claim `remote_human` without a session-scoped token path.
- Hook installation preserves DCG ahead of Longhouse, keeps telemetry
  fail-open, and keeps only the minimal permission hook fail-closed.
- Cursor permission prompts use Cursor-specific copy.
- Existing legacy `bypass` and `remote_approve` values retain their historical
  meaning for each provider and surface.
- Targeted real-provider coverage traverses the Longhouse launcher/hook boundary
  without requiring the broad local test suite.

## Deferred

- Longer human approval budgets and push/deep-link latency changes.
- Unix-socket permission transport.
- Autonomous AI policy decisions in place of the human.
- Broad permission-policy renaming for Claude and OpenCode.
