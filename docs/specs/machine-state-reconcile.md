# Machine State Reconcile

Status: Proposed launch blocker fix
Owner: local machine surface
Updated: 2026-04-14

## Goal

Make local Longhouse install and repair deterministic without importing a heavy
enterprise control-plane model.

Steal only three ideas:

- one authoritative machine state
- generated artifacts only
- one reconciler owns repair

Do not add:

- a second daemon
- a background reconcile loop
- policy layers
- a hosted control plane dependency

## Review Of Current Code

The current codebase already has the right product intent, but the local machine
path still violates it in a few concrete ways.

### Good seam that already exists

`server/zerg/services/local_runtime_installer.py` is the right place in spirit:

- it installs the engine
- installs the service
- installs hooks
- optionally installs `Longhouse.app`

That should survive.

### Where current state still drifts

The same durable machine target is currently writable in multiple places:

- `server/zerg/services/shipper/token.py`
  - `machine/target-url`
  - `machine/name`
- `server/zerg/cli/connect.py`
  - `_persist_selected_url()` also writes `config.toml`
- `server/zerg/cli/onboard.py`
  - writes `browser.default_url` and `shipper.api_url` again
- `server/zerg/cli/local_health.py`
  - resolves UI URL from `browser.default_url` first, then machine target
- `server/zerg/services/local_health.py`
  - compares `target-url`, `runner.env`, and service args as if all are peers

That means drift is not an edge case. The code currently normalizes around it.

### Where repair is still heuristic

The macOS app does not call an authoritative repair primitive. It reconstructs
repair inputs from a health snapshot:

- `desktop/.../LonghouseCLI.swift`
  - picks machine name from snapshot fields
  - picks repair URL from `runner_urls` or `stored_url`
- `desktop/.../ActionSink.swift`
  - launches `longhouse connect --install ...`
  - falls back to built-in setup if the CLI is missing

This is exactly the trust break:

- health detects one mismatch
- repair guesses from neighboring state
- the guessed inputs may not match the state that health just inspected

### Where shared install is not yet authoritative

`install_local_runtime()` currently writes some machine facts itself, but callers
still mutate adjacent state before or after it:

- `connect --install` writes machine state, then mirrors URL into `config.toml`
- `onboard` installs runtime, then separately writes config
- `serve` writes server-side runtime-host config into `config.toml`

So there is a shared installer seam, but not a single source of truth.

### Where shipping semantics are wrong for launch

The engine still treats most 4xx responses as payload failure:

- initial ship path:
  - `engine/src/shipper/mod.rs`
  - non-413 client errors advance offsets and skip replay
- replay path:
  - `engine/src/shipper/mod.rs`
  - non-413 client errors get dead-lettered
- transport classifier:
  - `engine/src/shipping/client.rs`
  - every 4xx except 429 becomes `ClientError`

That is too coarse for launch.

Wrong URL, auth drift, wrong host, route mismatch, or HTML from a bad endpoint
must not be treated the same as malformed ingest payload.

### Where version coherence is still weak

The repo now has:

- CLI install metadata in `~/.longhouse/install.json`
- runtime artifact acquisition in `server/zerg/services/runtime_artifacts.py`

But health still does not enforce one coherent local bundle generation across:

- CLI
- engine
- desktop app

Today those surfaces can move independently and health mostly reports fragments.

### Where provenance is missing

Machine-target writes are atomic, but not attributable:

- `save_zerg_url()`
- `save_machine_name()`
- `save_loaded_config()`

There is no small journal that answers:

- who changed the target
- which command wrote it
- when it changed

## Target Invariants

The launch-week design should enforce these rules:

1. `~/.longhouse/machine/state.json` is the only authoritative writable machine
   config for non-secret local install state.
2. `~/.longhouse/machine/device-token` remains separate because it is a secret,
   not normal machine config.
3. `~/.longhouse/config.toml` is runtime-host config only.
   It may keep `[server]` state. It must stop owning machine-agent target truth.
4. `browser.default_url` and `shipper.api_url` become deprecated mirrors during
   migration, then disappear as inputs.
5. Repair means: read canonical state, regenerate artifacts, restart, verify.
6. The app, CLI, onboarding, installer, and dogfood loop all call the same
   reconciler.
7. `runner.env` is not launch-critical machine truth.
   If runner stays enabled, it is either generated from the same machine state or
   treated as separate runner-only state.
8. Only irrecoverable payload failures enter dead-letter.
   Config drift and transport problems remain replayable.
9. Local health stays read-only.
   It must never repair by inference.

## Canonical Machine State

Proposed file:

- `~/.longhouse/machine/state.json`

Recommended MVP shape:

```json
{
  "schema_version": 1,
  "config_generation": "2026-04-14T22:18:09Z-4d3f7b8a",
  "runtime_url": "https://demo.longhouse.test",
  "machine_name": "cinder",
  "topology_intent": "connect-remote",
  "desktop_app_enabled": true,
  "written_by": "connect-install",
  "written_at": "2026-04-14T22:18:09Z",
  "desired_bundle_version": "0.1.11"
}
```

Notes:

- keep it tight
- no runtime health here
- no derived paths here
- no token here
- no runner-specific state unless the machine explicitly owns a runner

## Journal

Add:

- `~/.longhouse/machine/state-journal.jsonl`

Each append-only record should contain only non-secret config mutation data:

```json
{
  "written_at": "2026-04-14T22:18:09Z",
  "written_by": "connect-install",
  "config_generation": "2026-04-14T22:18:09Z-4d3f7b8a",
  "pid": 12345,
  "cwd": "/Users/davidrose/git/zerg",
  "argv": ["longhouse", "connect", "--install", "--url", "https://demo.longhouse.test"],
  "old": {
    "runtime_url": "http://127.0.0.1:8080",
    "machine_name": "cinder.local"
  },
  "new": {
    "runtime_url": "https://demo.longhouse.test",
    "machine_name": "cinder"
  }
}
```

This is intentionally small. It is not a full event store.

## Generated Artifacts

Everything below becomes derived output from `state.json` plus the token file:

- `~/.longhouse/machine/target-url`
- `~/.longhouse/machine/name`
- hook scripts and hook config
- launchd plist / systemd unit for the engine
- desktop app launchd plist
- any future app launch config
- optional runner config if this machine explicitly owns runner state

Migration rule:

- keep generating `target-url` and `name` first so the Rust engine and hooks do
  not need a same-day rewrite
- stop treating those files as authoritative inputs immediately

### Generated metadata

Generated artifacts should carry:

- `generated_by=longhouse`
- `config_generation=<...>`
- `source_state_hash=<sha256(state.json)>`

Use the format that fits the artifact:

- comment headers in `.env` and `.toml`
- environment variables in launchd/systemd
- plist comments are optional; explicit environment vars are better

Health can then say:

- service plist is generation X
- machine state is generation Y
- exact artifact drifted: `launchd_service`

## Reconcile Contract

Public repair verb stays:

- `longhouse connect --install`

Internal ownership changes to:

- write or update canonical machine state
- call one reconcile function

Canonical internal API:

```python
reconcile_machine_state(
    state,
    *,
    token,
    claude_dir,
    install_desktop_app,
    allow_artifact_download=True,
) -> ReconcileReport
```

Reconcile phases:

1. load canonical machine state
2. install or verify runtime artifacts for the desired bundle version
3. rewrite every generated artifact from state
4. restart or reload services
5. verify live coherence
6. return a structured report

Verification must check at least:

- state file exists and parses
- generated artifact metadata matches current generation
- engine service args match canonical machine name
- desktop app launch config matches current generation
- hooks exist and point at Longhouse-owned paths
- engine status is writable again after restart
- app bundle, engine binary, and CLI version agree on the bundle generation or
  fail loudly

## Health Contract

`server/zerg/services/local_health.py` should stop inferring truth from peer
files. It should read:

- canonical `state.json`
- live service state
- live engine status
- generated artifact metadata

It should preserve the existing surface shape where practical, but the source of
truth changes:

- `launch_readiness.stored_url` comes from `state.json`
- service mismatch means artifact drift against current generation
- `runner.env` becomes optional secondary evidence, not primary truth

The important product rule stays:

- health and repair must talk about the same object

That means the repair action should never reconstruct URL or machine name from a
snapshot. It should invoke the reconciler that reads canonical state directly.

## Failure Classes For Shipping

Replace the current `retry vs client error` split with three classes:

- `transport_retryable`
- `config_drift`
- `payload_irrecoverable`

Rule:

- only `payload_irrecoverable` may enter dead-letter

Initial mapping:

- network errors, 429, 5xx -> `transport_retryable`
- 401, 403, 404, 405, unexpected HTML, wrong-route proxy responses, version
  handshake mismatch -> `config_drift`
- 400 invalid gzip/zstd/json from Longhouse, 422 invalid ingest payload ->
  `payload_irrecoverable`
- 413 stays retryable until byte-based splitting resolves it

To make this robust, the ingest endpoint should return machine-readable error
codes for handled failures, for example:

- `invalid_content_encoding`
- `invalid_json`
- `invalid_payload`
- `managed_session_mismatch`
- `device_token_invalid`

If the response is not recognizable as Longhouse at all, treat it as
`config_drift`, not payload corruption.

## Version Coherence

Keep `install.json` for CLI acquisition metadata.
Do not overload it as machine runtime truth.

Add machine-runtime coherence to `state.json` and health:

- desired bundle version or generation lives in machine state
- reconcile installs engine and app to match it
- health compares:
  - CLI version
  - engine version
  - desktop app bundle version
  - last successful reconcile generation

Launch rule:

- dogfood, installer, and repair should leave one coherent bundle or report
  failure
- partial success is allowed as an intermediate filesystem fact, but not as a
  green health state

## Exact Files To Collapse

### New modules

- `server/zerg/services/machine_state.py`
- `server/zerg/services/machine_reconcile.py`

### Existing modules that should stop owning machine truth

- `server/zerg/services/shipper/token.py`
  - keep token helpers
  - move URL and machine-name authority into `machine_state.py`
- `server/zerg/cli/config_file.py`
  - keep runtime-host `[server]`
  - deprecate machine-target mirrors as inputs
- `server/zerg/cli/connect.py`
  - remove `_persist_selected_url()`
  - write canonical state, then reconcile
- `server/zerg/cli/onboard.py`
  - stop direct machine-target mirror writes
  - delegate to reconcile after topology choice
- `server/zerg/services/local_runtime_installer.py`
  - either become a thin wrapper over `machine_reconcile.py` or merge into it
- `server/zerg/services/local_health.py`
  - compare artifacts against canonical machine generation
- `server/zerg/cli/local_health.py`
  - resolve UI target from canonical machine state or explicit local serve mode
- `desktop/.../LonghouseCLI.swift`
  - stop deriving `--url` and `--machine-name` from snapshots
- `desktop/.../ActionSink.swift`
  - call a canonical reconcile entrypoint only

### Engine/runtime changes

- `engine/src/config.rs`
  - migrate from reading legacy `target-url` and `name` directly to reading
    generated compat output first, then `state.json`
- `engine/src/shipping/client.rs`
  - return richer failure classes
- `engine/src/shipper/mod.rs`
  - stop dead-lettering config drift

## Staged Migration

### Phase 0: Stop adding new writable mirrors

- add `machine_state.py`
- start writing `state.json` and the journal
- keep generating legacy `target-url` and `name`
- keep reading legacy files only as migration fallback

### Phase 1: Make reconcile authoritative

- add `machine_reconcile.py`
- route `connect --install`, desktop repair, dogfood refresh, and onboarding
  through it
- remove any post-install mirror writes outside reconcile

### Phase 2: Make health compare state to artifacts

- local health reads canonical state first
- report generation mismatch explicitly
- stop using `runner.env` as launch-critical truth

### Phase 3: Fix shipping failure classes

- add structured ingest error codes
- map config/auth/wrong-host failures to replayable drift, not dead-letter

### Phase 4: Enforce bundle coherence

- record last successful reconcile generation
- compare CLI, engine, and app versions in health
- mark split-brain bundle state as degraded or broken

### Phase 5: Remove deprecated mirrors

- stop reading `browser.default_url` and `shipper.api_url` for machine target
- remove `target-url` and `name` only after engine and hooks no longer depend on
  them

## Launch-Week First Cuts

If time only allows the highest-value fixes before launch, do these first:

1. introduce canonical `state.json` and journal
2. make `connect --install` the only repair writer
3. change the menu bar repair action to call canonical reconcile, not guessed
   args
4. stop dead-lettering config drift and auth drift
5. add generation mismatch reporting to local health

That is enough to remove the current split-brain failure mode without building a
full controller system.
