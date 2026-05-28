---
name: managed-provider-cli
description: Longhouse managed provider CLI control paths for Claude, Codex, Gemini, and similar CLIs. Use when investigating or changing managed sessions, provider binary ownership, `longhouse claude`, `longhouse codex`, bridge/relay/hook behavior, local-health liveness, reattach behavior, stale provider runtimes, or AGENTS.md guidance about managed vs unmanaged sessions.
---

# Managed Provider CLI

Use this skill when a task touches how Longhouse starts, observes, steers, or repairs provider CLIs. The core rule: Longhouse manages session control, not provider binary distribution.

## Mental Model

- **Provider CLI**: an upstream executable the user installs separately, such as `claude`, `codex`, or `gemini`.
- **Managed session**: Longhouse owns the control path. It does not imply Longhouse owns, vendors, pins, patches, or updates the provider binary.
- **Unmanaged session**: Longhouse ingests or discovers a bare provider CLI session but does not own a live control path.
- **Machine Agent**: `longhouse-engine`; ships local events, owns runtime hooks/state, and may run provider-specific bridge processes.
- **Runtime Host**: FastAPI/web/database product runtime. It stores session state and exposes `/api/agents/*`.

## Provider Paths

### Claude

- `longhouse claude` relies on Claude's native channel/MCP/stdin control path.
- Claude channel send, interrupt, and active-turn steer are first-class local
  control operations. Steer uses `claude-channel send --meta intent=steer` and
  Runtime Host must gate explicit `intent=steer` on a fresh active runtime
  phase; idle channel injection is not steer.
- Machine Agent remote launch uses the `claude.launch` support bit and must
  run stock Claude under a PTY wrapper with
  `--dangerously-load-development-channels server:longhouse-channel` and wait
  for Claude channel state before Runtime Host records the launch as live.
  Longhouse's channel is a private MCP server, not an Anthropic allowlisted
  channel plugin; do not silently fall back to unmanaged/import-only launch if
  Claude rejects this control path.
- Claude hook tokens must be passed through process env, never embedded in
  shell commands or PTY launch logs.
- No detached bridge daemon, bridge state file, or flock sidecar should be required for Claude liveness.
- Claude liveness in local health comes from process scanning, especially `local_health._collect_managed_sessions_by_process`.

### Codex

- `longhouse codex` resolves the user's stock upstream `codex` from PATH by default.
- `--codex-bin` and `LONGHOUSE_CODEX_BIN` are explicit debug/operator overrides, not the normal install path.
- The Python CLI starts `longhouse-engine codex-bridge`.
- The Rust bridge starts `codex app-server`, fronts it with `engine/src/codex_ws_relay.rs`, and attaches the TUI with `codex --enable tui_app_server --remote ...`.
- Bridge state, logs, lock sidecars, and IPC sockets live under `~/.longhouse/managed-local/codex-bridge/` unless overridden.
- Hook scripts such as `longhouse-codex-hook.sh` are Longhouse hook scripts, not provider binaries.

Codex launch modes:

- **TUI-attached managed**: long-running Codex app-server plus visible `codex` TUI attached through `--remote`.
- **Detached-UI managed**: long-running Codex app-server and Longhouse bridge, but no visible terminal TUI. Browser/iOS remote launch uses this mode so the session remains steerable from Longhouse without opening a terminal window.
- **One-shot/batch**: prompt is passed to a provider process and the process exits after one turn. Do not call this "headless" in managed-session code or docs; it is a different execution model and is not equivalent to detached-ui managed control.

Bridge state compatibility:

- Treat `docs/specs/managed-codex-state-compat.md` as the contract before changing bridge state fields.
- New bridge writers persist detached-UI managed sessions as `launch_mode=detached_ui`; readers still tolerate older dogfood `headless` state.
- Reapers must skip live-bridge reaping for unknown launch modes or future bridge state schema versions.

Hard Codex contract:

- Do not ship a Codex runtime payload.
- Do not install or generate `longhouse-codex`.
- Do not keep or recreate `~/.longhouse/runtimes/codex`.
- Do not download Codex release assets or patch a custom Codex fork.
- Install/repair code may remove old managed-Codex artifacts conservatively.

### OpenCode

- `longhouse opencode` starts stock upstream `opencode serve` on localhost
  through Longhouse's `opencode_server_bridge`, then attaches the TUI with
  stock `opencode attach`.
- OpenCode server-bridge send and interrupt are first-class local control
  operations. Active-turn steer is not advertised until OpenCode exposes and
  proves a true mid-turn injection semantic.
- Bridge state lives under `~/.claude/managed-local/opencode-server/` and
  stores the local server password in a 0600 state file so `longhouse
  opencode-channel attach/send/interrupt` can reconnect without printing
  secrets. Runtime hook tokens must not be written to the bridge state or
  server log.
- OpenCode launch is idempotent per Longhouse session id. A retry must reuse a
  live state file instead of spawning a second `opencode serve`.
- Machine Agent should only advertise `opencode.*` control support when the
  stock `opencode` binary is present on PATH.

### Gemini And Future CLIs

- Start from the same ownership rule: Longhouse can own the wrapper/control path, but the provider CLI remains user-owned unless the product decision explicitly changes.
- Do not infer one provider's liveness/control model from another provider. Split behavior when the provider mechanics differ.
- Antigravity has a managed local wrapper plus a hook-inbox adapter. The named
  control plane is `antigravity_hook_inbox`; do not advertise
  `antigravity.send` until a real `agy` loop canary proves active
  `PreInvocation`/`PostInvocation` hooks claim pending input. Do not advertise
  Antigravity remote launch, reattach, interrupt, or active-turn steer until a
  stable provider surface proves those semantics.

## Workflows

### Debug Version Or Binary Drift

1. Compare `command -v <provider>` and `<provider> --version` with the process actually attached to the managed session.
2. For Codex, check whether `LONGHOUSE_CODEX_BIN` or `--codex-bin` is forcing a non-PATH binary.
3. Check for stale legacy artifacts:
   - `~/.local/bin/longhouse-codex`
   - `~/.longhouse/runtimes/codex`
4. Verify the wrapper is launching the provider binary directly, not a Longhouse-owned payload.
5. If stale Codex runtime artifacts exist, prefer the existing local runtime cleanup path over ad hoc deletion.
6. Treat Sauron provider release status and local provider live proof as
   separate feeds. Fresh green local proof can demote
   `yellow/insufficient_coverage` to advisory-only for local health, but it
   never suppresses red release blockers.

### Debug Codex Bridge Failures

1. Inspect `~/.codex/log/codex-tui.log`.
2. Inspect the bridge `.json` state and `.log` under `~/.longhouse/managed-local/codex-bridge/`.
3. Inspect rollout JSONL for the thread before trusting `last_turn_status`.
4. Check app-server `readyz` and whether the relay URL was logged.
5. For `longhouse-engine codex-bridge start` repros, prefer `--isolation-root <tmp-dir>`. It maps bridge state to `<tmp-dir>/codex-bridge` and Longhouse state to `<tmp-dir>/longhouse`. If you use lower-level flags, set both `--state-root` and `--longhouse-home`; `--state-root` alone is intentionally rejected on start.
6. Codex v0.133 can print `No active thread is available.` during fresh remote-TUI startup before `StartupThreadStarted` installs the active thread. Verify bridge state, thread id, readyz, and rollout progress before treating that line as a managed-control failure.
7. Do not kill Codex processes by broad `ps | grep codex app-server` matches. Use the bridge state file's exact `session_id`, `pid`, `app_server_pid`, `app_server_pgid`, and `ws_url`, then stop through `longhouse-engine codex-bridge stop --session-id ...` or an isolated repro state root.

### Change Managed Provider Code

Read the relevant path before editing:

- Codex CLI wrapper: `server/zerg/cli/codex.py`
- Codex bridge: `engine/src/codex_bridge.rs`
- Codex WS relay: `engine/src/codex_ws_relay.rs`
- Runtime installer/cleanup: `server/zerg/services/local_runtime_installer.py`
- Local health/liveness: `server/zerg/services/local_health.py`
- Managed session state: `engine/src/state/managed_session_state.rs`
- Phase ledger: `engine/src/state/session_phase.rs`

After changes:

- Run focused tests for the touched layer.
- For Codex launcher or cleanup changes, run `cd server && uv run pytest tests_lite/test_codex_cli.py tests_lite/test_local_runtime_installer.py`.
- For bridge/relay changes, run `make test-engine`.
- For local runtime install changes that affect the dogfood machine, run `make dogfood-refresh`.

## Naming Rules

- Use `managed` for Longhouse-owned control paths.
- Use `unmanaged` for imported/discovered sessions without live control ownership.
- Use `detached-ui managed` for a long-running managed provider session with no visible terminal TUI attached.
- Use `one-shot` or `batch` for prompt-and-exit execution.
- Avoid `headless` for Codex managed-session lifecycle unless you explicitly mean a non-GUI environment, headless browser, or other conventional non-session usage.
- Use `live`, `reattachable`, `phase-known`, and `running` separately; do not collapse them into one status.
- In local-health JSON, keep `control_path`, `liveness_model`, and `state` separate; do not infer managed/unmanaged ownership from process liveness or attached/detached state.
- Avoid naming constants or paths as if Longhouse owns a provider binary when the behavior is only wrapper config or update-check suppression.

## Fallback Rule

Avoid hidden fallbacks. If a fallback remains necessary, make it explicit in logs/UI or gate it behind a named debug/operator path. Never use a fallback to silently switch from user-owned provider CLI execution to a Longhouse-owned provider runtime.
