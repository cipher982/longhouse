# Provider CLI Contracts And Codex Release Canaries

Status: Draft
Owner: Machine Agent + managed provider CLI surfaces
Updated: 2026-05-27
Related: `managed-provider-control-matrix.md`, `managed-codex-prestarted-tui-attach.md`, `managed-codex-state-compat.md`, `managed-codex-liveness.md`, `remote-session-launch.md`, `distribution-update-loop.md`

## Context

Longhouse depends on upstream provider CLIs, but it does not own those binaries.
For Codex, Longhouse owns the managed control path around the user's stock
upstream `codex` executable: Python CLI wrapper, Rust bridge, WebSocket relay,
bridge state, local health, reaper, runtime event projection, and browser/iOS
control surfaces.

The Codex 0.133 incident exposed the real contract: upstream Codex is not just
a binary we shell out to. It is a moving protocol and UI dependency. A source
diff found that fresh remote TUI startup became asynchronous; a behavior repro
showed startup events could route before Codex had installed an active thread,
rendering `No active thread is available.` The Longhouse fix was to avoid that
fragile fresh-start path: create the thread through the bridge, then attach the
stock TUI to the bridge's active thread with `codex --remote <bridge_ws_url>`.

This spec turns that lesson into a reusable contract for provider CLI
integration and a concrete release canary plan for Codex.

## Goals

1. Make provider CLI dependencies explicit enough that agents do not rediscover
   them from implementation details.
2. Make Sauron emit a structured Green/Yellow/Red artifact per Codex release,
   with versions, source-review findings, canary results, failure codes, and
   evidence paths that downstream surfaces can consume without reading agent
   reasoning.
3. Keep pre-launch code honest: no ambiguous compatibility flags, no hidden
   fallbacks, no Longhouse-owned Codex binary path.
4. Preserve cheap reader tolerance for old dogfood local state when deleting it
   would interrupt debugging, while forbidding new legacy writer paths.
5. Keep contract tests close to Longhouse code and release canaries close to
   the upstream dependency they validate.

## Non-Goals

- No pinning, vendoring, patching, or redistributing Codex.
- No generic provider abstraction that hides provider-specific mechanics.
- No public backward-compatibility promise before launch.
- No expensive canary that must run on every local `make test`.
- No second session-ingest system outside Longhouse's existing runtime and
  `/api/agents/*` surfaces.
- No assumption that every upstream Codex regression is a Longhouse bug. Yellow
  can mean "upstream changed or regressed; Longhouse tolerates it for now."

## Operating Principles

### Provider Binary Ownership

Longhouse launches the user's provider CLI. It may pass explicit arguments and
environment variables, but it must not silently swap in a Longhouse-owned
runtime. Debug overrides such as `--codex-bin` and `LONGHOUSE_CODEX_BIN` are
operator paths, not product install paths.

### Named Lifecycle Axes

Do not collapse independent lifecycle questions into one flag:

- **Binary ownership:** user-owned upstream vs Longhouse-owned artifact.
- **Thread creation:** TUI-created vs bridge-created.
- **Launch mode:** TUI-attached vs detached-ui managed.
- **Control path:** managed bridge/channel vs unmanaged transcript ingest.
- **Process liveness:** bridge alive, app-server alive, TUI attached.
- **Session phase:** idle, running, needs user, degraded, ended.

When a new capability needs two axes, add two named fields or flags. Do not add
a convenience alias that recouples them.

### Protocol Evidence Beats Assumptions

Provider protocol JSONL is contract evidence. Canaries should preserve method
names, notification ordering, redacted response shapes, and server-request
shapes. Release review should compare those shapes against the last known good
release instead of relying only on source-reading or terminal-visible behavior.

### Pre-Launch Compatibility Rule

Before launch, prefer deletion over compatibility surfaces. Accept old local
dogfood state in readers only when doing so prevents self-inflicted debugging
interruptions. New writers, CLIs, docs, and tests should use the desired final
shape. Reader tolerance applies to old on-disk state; deletion applies to
writer code, CLI flags, docs, and tests.

## Current Codex Contract

### Stock Binary

- Default binary: `codex` resolved from `PATH`.
- Explicit debug overrides: `--codex-bin`, `LONGHOUSE_CODEX_BIN`.
- Forbidden: `longhouse-codex`, `~/.longhouse/runtimes/codex`, downloaded Codex
  release assets, patched Codex forks.

### Managed TUI-Attached Launch

Local `longhouse codex` must:

1. POST a managed-local Longhouse session for provider `codex`.
2. Start `longhouse-engine codex-bridge start` with:
   - `--create-initial-thread`
   - default `--launch-mode tui`
   - user-selected cwd/model/reasoning settings
3. Require bridge startup JSON to include non-empty `ws_url` and `thread_id`.
4. Attach the visible stock TUI with:

```text
codex -c check_for_update_on_startup=false --enable tui_app_server --remote <bridge_ws_url>
```

The TUI attach path must rely on the bridge-prestarted active thread, not on
`codex resume <thread_id>`, because Codex 0.133 resolves `resume` through local
rollout files that bridge-created threads may not have in the visible user's
session tree.

### Detached-UI Managed Launch

Browser/iOS remote launch must start the same bridge/app-server control path
without a visible TUI:

```text
create_initial_thread=true
launch_mode=detached-ui
```

Bridge writers persist this as `launch_mode=detached_ui`. Readers may tolerate
older dogfood `headless` state, but writers must not emit it.

### Upstream Codex Surfaces We Depend On

Longhouse currently depends on these upstream Codex behaviors:

- `codex app-server` can be started as a long-running local process.
- The app-server exposes a WebSocket endpoint and `readyz` health check.
- WebSocket startup logs include a parseable listen URL announcement.
- JSON-RPC `initialize` succeeds before thread operations.
- JSON-RPC `thread/start` returns `thread.id` and usually `thread.path`.
- JSON-RPC `thread/resume` can subscribe Longhouse to an existing thread over
  the app-server protocol.
- `thread/started` notifications identify the primary thread.
- `codex --enable tui_app_server --remote <ws_url>` attaches a visible TUI to
  the app-server's active bridge-prestarted thread.
- App-server startup accepts the Longhouse-required feature flags:
  - `--enable tui_app_server`
  - `--enable hooks`
  - `--enable exec_permission_approvals`
  - `--enable request_permissions_tool`
- Server-to-client request methods remain answerable by Longhouse:
  - `item/commandExecution/requestApproval`
  - `item/fileChange/requestApproval`
  - `item/permissions/requestApproval`
  - `item/tool/requestUserInput`
  - `mcpServer/elicitation/request`
  - legacy `applyPatchApproval` and `execCommandApproval`
- `LONGHOUSE_MANAGED_SESSION_ID` reaches Codex hook context.
- Rollout JSONL files remain readable enough for thread path binding and
  terminal turn status checks.
- Rollout files may appear after `thread/started`; Longhouse may retry waiting
  for on-disk materialization.
- `--dangerously-bypass-approvals-and-sandbox`, `--model`, and
  model reasoning effort settings retain their existing CLI meanings.
- The config key `check_for_update_on_startup=false` remains accepted through
  Codex `-c`.

## Source Review Checklist For Sauron

For every upstream Codex release, review diffs in these areas first:

- TUI startup and thread routing, especially fresh vs resume startup.
- App-server protocol methods and JSON shapes around `initialize`,
  `thread/start`, `thread/read`, `thread/list`, `turn/start`, `turn/interrupt`,
  and notifications.
- CLI argument parsing for `app-server`, `resume`, `--remote`, `--enable`, model
  flags, sandbox, and approval policy.
- Skills/plugin startup, app-scoped events, and any code that can emit commands
  before an active thread exists.
- Rollout/transcript file naming, source metadata, subagent/thread-spawn
  markers, and schema changes.
- Approval/server-request event names and auto-approval flows.
- Any startup update checks, account/model loading, or background tasks that can
  emit visible TUI output before the managed thread is attached.

Each checklist item should be marked `unchanged`, `changed`, or `risky`, with a
short evidence pointer. The release output should classify each release as:

- **Green:** no contract risk found and canaries pass.
- **Yellow:** source risk or canary warning; Longhouse can keep running but the
  issue needs triage before upgrade recommendation.
- **Red:** canary failure or direct break in a required contract; block upgrade
  recommendation and open a Longhouse fix task.

Sauron should publish a single structured artifact per release:

```json
{
  "provider": "codex",
  "codex_version": "0.134.0",
  "codex_bin": "/opt/homebrew/bin/codex",
  "longhouse_commit": "abc1234",
  "verdict": "yellow",
  "failure_code": null,
  "recommendation": "investigate_before_upgrade",
  "source_review": {
    "tui_startup": "changed",
    "app_server_protocol": "unchanged",
    "cli_args": "unchanged",
    "rollout_format": "unchanged"
  },
  "canaries": {
    "binary_identity": { "status": "pass" },
    "raw_fresh_remote": { "status": "warn", "evidence": "..." },
    "managed_tui_attach": { "status": "pass", "turn_status": "completed" },
    "detached_ui": { "status": "pass" },
    "fake_app_server": { "status": "pass" }
  },
  "evidence_root": "..."
}
```

Downstream action:

- **Green:** update the recommended provider version; no user-facing warning.
- **Yellow:** keep the recommended version at the previous Green; local-health
  may show a low-severity note with the artifact link.
- **Red:** block upgrade recommendation, open a Longhouse fix task, and warn
  only users already running the affected provider version.

## Provider Release Status Signal

Sauron owns the provider-release status artifact. Longhouse consumers should
read the structured artifact, not Sauron's raw reasoning transcript.

Initial product contract:

- Sauron publishes one latest-status JSON document per provider.
- `scripts/qa/provider-release-profile-canary.py` emits the shared provider
  profile artifact for any managed provider in
  `server/zerg/config/managed_provider_contracts.json`.
- `scripts/qa/codex-provider-release-canary.py` remains the Codex-specific
  live bridge/TUI canary suite.
- Runtime Host and local-health may cache it and expose:
  - provider
  - upstream version
  - verdict
  - recommendation
  - failure code
  - evidence URL
  - generated timestamp
- Local-health evaluates every managed provider in the contract registry. A
  provider with no configured artifact is reported as `not_configured` and does
  not affect health; configured Yellow/Red artifacts can warn or block only
  when they match the locally installed provider version.

The exact storage path remains a deployment detail, but the JSON schema should
be stable before any UI or upgrade prompt depends on it.

## Behavioral Canaries

Canaries should run with an isolated home and bridge state root. They should
preserve raw logs and JSONL evidence on failure.

### 0. Binary Identity Canary

Purpose: make sure the canary is testing the user's stock upstream Codex path,
not a forbidden Longhouse-owned runtime or debug override.

Flow:

1. Record `command -v codex` and `codex --version`.
2. Fail if `LONGHOUSE_CODEX_BIN` is set outside an explicit debug lane.
3. Fail if the Codex path matches forbidden managed-runtime artifacts:
   - `longhouse-codex`
   - `~/.longhouse/runtimes/codex`
4. Include binary path and version in the Sauron artifact.

### 1. Upstream Fresh Remote TUI Warning Canary

Purpose: detect upstream startup regressions early, even when Longhouse avoids
the fresh path.

Flow:

1. Start raw stock `codex app-server`.
2. Attach raw stock `codex --enable tui_app_server --remote <ws_url>` under a
   short TUI recording window.
3. Capture the TUI session log and stderr.
4. Warn if visible startup output contains known active-thread errors or if
   app-scoped startup events route through active-thread-only errors.

This canary is upstream-only: no Longhouse bridge, no Longhouse state, no
Longhouse home. It is advisory. It can flag upstream regressions without
failing the Longhouse contract when managed TUI attach still works.

### 2. Managed TUI Attach Contract Canary

Purpose: prove the user-facing `longhouse codex` startup contract against the
current stock Codex binary.

Flow:

1. Run `longhouse-engine codex-bridge start` with `--isolation-root`,
   `--create-initial-thread`, and `--json`.
2. Assert returned JSON includes:
   - non-empty `ws_url`
   - non-empty `thread_id`
   - state file exists
   - state `launch_mode=tui`
3. Attach stock TUI with:

```text
codex -c check_for_update_on_startup=false --enable tui_app_server --remote <ws_url>
```

4. Record the TUI session briefly.
5. Assert no visible startup error such as `No active thread is available.`
6. In scheduled or manual lanes, send a tiny synthetic prompt and assert the
   turn reaches a terminal status instead of remaining stuck.
7. Stop the bridge through `longhouse-engine codex-bridge stop --session-id`.

This is the primary contract canary.

### 3. Detached-UI Bridge Contract Canary

Purpose: prove browser/iOS remote launch can create a steerable managed session
without a visible TUI.

Flow:

1. Run `longhouse-engine codex-bridge start` with `--isolation-root`,
   `--create-initial-thread`, `--launch-mode detached-ui`, and `--json`.
2. Assert state `launch_mode=detached_ui`.
3. Assert bridge IPC exists and `readyz` succeeds.
4. Optionally send a tiny prompt only in scheduled or manual canary lanes, not
   every source-review run.
5. Stop by session id and verify app-server child cleanup.

### 4. Fake App-Server Unit Contract

Purpose: keep fast CI coverage independent of real Codex.

Use a fake app-server or existing `codex-app-server-canary` harness to assert:

- prestart mode refuses ready-without-thread
- launch-mode persistence round-trips `tui` and `detached_ui`
- writer code never emits `headless`
- managed TUI attach argv contains `--enable tui_app_server --remote <ws_url>`
  without `resume <thread_id>`
- process-scan detection matches `codex --remote <ws_url>` and still tolerates
  older `codex resume ... --remote <ws_url>` processes for dogfood cleanup
- protocol-shape failure evidence includes method name, redacted shape, and
  evidence path

This canary belongs in normal CI once stable.

## Protocol Shape Fingerprints

Managed canaries should emit redacted shape fingerprints for:

- `initialize` response
- `thread/start` response
- `thread/resume` response when exercised
- `thread/started` notification
- first `turn/started` and terminal turn notification when a prompt is sent
- server-request approval and user-input methods seen during the run

Shape fingerprints should include object keys and value types, not raw user
content. Sauron should diff fingerprints against the last Green release and
include `unchanged`, `changed`, or `risky` in the release artifact.

## Evidence And Artifacts

On failure, canaries should preserve:

- Codex version and binary path.
- Longhouse commit SHA and engine build identity.
- Exact argv for app-server, bridge, and TUI.
- Bridge state JSON and bridge log.
- TUI recording log, stderr, and visible transcript excerpt.
- App-server protocol JSONL if proxied.
- A short machine-readable failure code.

Default artifact root:

```text
.build/canaries/codex/<codex-version>/<timestamp>/
```

Do not ship raw user prompts or real repo-sensitive content in public CI
artifacts. Release canaries should use a temporary cwd with synthetic files.

## Implementation Roadmap

1. Convert this spec into the canonical checklist for Sauron Codex release
   review.
2. Expand `validate-managed-codex-contract` or add a sibling target for source
   checks that forbid reintroducing packaged Codex runtimes, `--start-thread`,
   `longhouse-codex`, or writer-side `headless`.
3. Add a manual/scripted managed TUI attach canary using an isolated home.
4. Wire Sauron release jobs to run source review plus behavioral canaries and
   emit Green/Yellow/Red output.
5. Promote the fake app-server contract subset into normal CI once stable.
6. Repeat the same provider-contract shape for Claude and Gemini only where
   their mechanics actually need it.

## Open Questions

- Should the raw fresh remote TUI warning canary ever become blocking, or stay
  advisory when the managed TUI attach contract passes?
- Should model-invoking canaries run nightly, on every Codex release, or only
  manually? Default until decided: scheduled/manual only, not every cheap source
  review run.
- Should canary artifacts live only in Sauron-owned storage, or should local
  manual runs also keep `.build/canaries/...` artifacts? Default until decided:
  both.
- What exact Sauron storage path should host the provider-release status
  artifact?
