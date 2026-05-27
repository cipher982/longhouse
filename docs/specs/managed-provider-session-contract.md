# Managed Provider Session Contract

Status: Pre-launch hardening
Owner: Machine Agent + provider launchers + local-health
Updated: 2026-05-27
Related: `machine-local-managed-session-state.md`, `managed-codex-state-compat.md`, `session-liveness-honesty.md`

## Goal

Make managed provider sessions explainable when the provider process, its
workspace, or its local bridge state becomes invalid before Longhouse code can
run.

The design target is:

- Longhouse records the local execution contract at managed-session launch
- local-health verifies that contract directly
- provider logs and transcripts are supporting evidence, not the primary truth
- user-facing repair copy names the broken contract piece, not the raw provider
  error string

## Incident That Motivated This

A Claude Code session ran from `/private/tmp/lh-opencode-e2e`. That directory
was later deleted while the session was still active. Claude's hook runner then
reported:

```text
ENOENT: no such file or directory, posix_spawn '/bin/sh'
```

`/bin/sh` existed and the Longhouse hook script ran correctly when invoked
directly. The provider hook runner failed before the hook script started because
its process cwd no longer existed. The error named the Longhouse hook command,
so it looked like Longhouse broke.

This is the broader class: provider sessions can lose the environment they were
launched with, and the provider's own error framing can make Longhouse look like
the failing component.

## First-Principles Contract

A managed provider session is the tuple:

- provider identity: provider name, provider binary path, provider version
- Longhouse identity: session id, launch mode, Longhouse build id
- execution identity: launch cwd, canonical cwd, cwd file identity when the
  platform can provide one
- control identity: bridge/control state path, control plane kind, live-control
  capability
- provenance: launch timestamp and last contract verification timestamp

Longhouse owns this contract because Longhouse is the component that launches
the managed session and later asks users to trust the control path. Provider
transcripts, hook errors, and bridge logs remain evidence, but they should not
be the only way local-health decides what failed.

## Failure Taxonomy

Use specific reason codes. Do not collapse these into a generic
`managed_session_unhealthy` reason.

| Reason | Meaning | Primary action |
| --- | --- | --- |
| `provider_session_cwd_missing` | Recorded cwd no longer exists. | Recreate the directory, detach the session, or reattach from an existing cwd. |
| `provider_session_cwd_replaced` | Recorded cwd path exists but file identity changed. | Verify the workspace was intentionally recreated before reattaching. |
| `provider_hook_spawn_failed` | Provider could not spawn the configured hook process. | Inspect cwd, shell availability, permissions, and provider hook runner logs. |
| `bridge_state_path_missing` | Recorded Longhouse bridge/control state path is missing. | Restart or detach the managed session; repair machine state if repeated. |
| `provider_binary_changed` | Current provider binary/version differs from launch. | Restart the session under the current provider CLI. |
| `legacy_bridge_state_location` | Longhouse read managed state from a legacy compatibility path. | Let the session quiesce, then restart under the current Longhouse build. |

## Contract Storage

Store one JSON contract file per managed session under Longhouse-owned state:

```text
~/.longhouse/managed-local/contracts/<provider>/<session-id>.json
```

The file is not secret-bearing. It may include paths and command provenance, so
keep permissions private by default.

Minimum schema:

```json
{
  "schema_version": 1,
  "session_id": "uuid-or-provider-session-id",
  "provider": "codex",
  "launch_mode": "tui",
  "created_at": "2026-05-27T15:00:00Z",
  "longhouse_build": "2026.05.27+abcdef",
  "provider_binary": {
    "path": "/opt/homebrew/bin/codex",
    "source": "PATH",
    "version": "codex 0.133.0"
  },
  "workspace": {
    "cwd": "/Users/test/git/longhouse",
    "canonical_cwd": "/Users/test/git/longhouse",
    "file_identity": "dev=...,ino=..."
  },
  "control": {
    "kind": "codex_bridge",
    "state_path": "/Users/test/.longhouse/managed-local/codex-bridge/session.json"
  }
}
```

The contract is not a replacement for `managed_session_state`. The existing
SQLite row owns current phase/workspace truth. The contract owns launch-time
environment and control provenance.

## local-health Verification

Non-fast local-health should:

1. Read managed-session contracts.
2. Verify the recorded cwd still exists.
3. Verify cwd file identity when available.
4. Verify the recorded bridge/control state path when the provider uses one.
5. Compare the current provider binary/version to the launched binary/version.
6. Merge provider transcript diagnostics as corroborating evidence.

Fast local-health should skip transcript scans and expensive provider version
commands, but it may still read small local contract files if needed for a
sessions-only status surface.

## Provider Scope

Claude:

- no detached Longhouse bridge state file is required for liveness
- contract still records cwd, provider binary, and Claude channel/control
  provenance
- Claude transcript hook diagnostics are secondary evidence for
  `provider_hook_spawn_failed` and `provider_session_cwd_missing`

Codex:

- contract records the `codex-bridge` state path and provider binary used by
  the bridge
- bridge state remains under `~/.longhouse/managed-local/codex-bridge/`
- legacy `.claude/managed-local` state is compatibility-only

OpenCode:

- contract records the `opencode` bridge state path and plugin config content
  path
- OpenCode managed sessions are observe-only at the live-control layer today;
  the contract should still verify cwd and bridge state

Antigravity:

- start with cwd and provider binary/version only unless a durable control
  artifact earns its place

## QA And CI

Add automated coverage at three layers.

Unit tests:

- contract read/write round trip
- cwd missing
- cwd replaced
- bridge state missing
- provider binary changed
- reason-code to remediation mapping

Integration tests:

- launch a synthetic managed session contract in a temp workspace
- delete the workspace
- run local-health
- assert `provider_session_cwd_missing` without requiring provider transcripts
- recreate the path with a different file identity and assert
  `provider_session_cwd_replaced`
- delete the bridge state path and assert `bridge_state_path_missing`

QA/CI guard:

- scan repo QA scripts for managed provider launches from temp directories
- require explicit teardown ordering: stop/detach managed session before
  deleting the temp cwd
- keep the guard static and conservative; it should catch obvious regressions
  without pretending to prove arbitrary shell control flow

## Phased Build Plan

Phase 0: ship the diagnostic baseline.

- Claude transcript scan classifies hook shell spawn ENOENT plus missing cwd
- local-health surfaces a specific headline and action

Phase 1: add the contract model.

- Python contract helpers for read/write/list/verify
- focused tests for contract verification and reason mapping
- local-health reads contracts and emits provider-neutral reasons

Phase 2: write contracts from launchers.

- Claude, Codex, OpenCode, and Antigravity launch paths write contract files
- Codex/OpenCode include bridge state paths
- provider version capture is best-effort and never blocks launch

Phase 3: add integration QA and CI.

- synthetic end-to-end local-health tests for missing/replaced cwd and missing
  bridge state
- static QA guard for temp managed-session cwd teardown ordering
- CI hooks run the guard with other lightweight script tests

## Non-Goals

- Do not wrap or replace provider hook runners.
- Do not auto-recreate deleted workspaces.
- Do not make transcript scanning the primary detector.
- Do not write new Longhouse-owned state under provider-owned homes such as
  `~/.claude`.
- Do not introduce a generic contract framework. This is a small local
  managed-session contract because the launch loop needs it.
