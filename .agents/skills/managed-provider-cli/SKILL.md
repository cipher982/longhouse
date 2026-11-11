---
name: managed-provider-cli
description: Change or investigate how Longhouse launches, observes, steers, or repairs provider CLIs (Claude, Codex, OpenCode, Pi, OMP, Cursor, Antigravity). Use for managed vs unmanaged sessions, `longhouse claude|codex|opencode|pi|omp`, bridge/relay/hook/channel behavior, SEND vs STEER delivery, local-health liveness, reattach, provider binary drift, or stale Codex runtime artifacts.
---

# Managed Provider CLI

Longhouse manages session control, not provider binary distribution. The user
installs the provider CLI; Longhouse wraps, observes and steers it.

- Mode definitions (Shadow, Helm, Console): `ARCHITECTURE.md`, "Session modes".
- What each provider supports and how proven it is: `schemas/managed_providers.yml`
  (read the provider's cell before investigating a gap).
- Never run a provider CLI on the maintainer's Mac, even with a scratch `HOME`
  (workspace `AGENTS.md`, "Providers and the factory"). Use fixture lanes
  (`make test-*`), the bench (`scripts/ops/bench.sh run ...`) or the factory. On
  the bench, a `cursor-agent` with a relocated `HOME` needs
  `AGENT_CLI_CREDENTIAL_STORE=file`.

## Input semantics: SEND and STEER

The sender picks the semantic. The target's phase decides only when the message
lands, never whether it survives. Full contract:
`control-plane/docs/specs/peer-input-delivery-contract.md`.

| Semantic | Intents | Delivery | Receipt |
|---|---|---|---|
| SEND | `queue`, `auto` | durable, ordered, dispatched at a turn boundary | `queued`, then `delivered` when the provider accepted it |
| STEER | `steer` | enters the running turn, provider-bounded | `delivered` on ack, or `409 steer_requires_active_turn` when no turn is running |

- A SEND to a mid-turn target parks a durable receipt; the turn-boundary drain
  delivers it. The provider's own mid-turn queue (OMP/Pi `deliverAs: "followUp"`)
  lives in process memory and dies with the turn, so Longhouse does not use it.
- `delivered` means the provider accepted the input, not that the model saw it.
  Confirm with `longhouse-server tail <id>`.
- Agent-to-agent messages use the coordination tools (`send`, `inbox`, `reply`),
  which carry the sender's identity. Claude, Codex and OpenCode Helm sessions list
  them from launch; until registration recovers they answer `registration_pending`,
  then work without a restart (the launcher writes a late-token file). If the
  launcher exits first (detached launch) they stay pending until relaunch. OMP gets
  its credential through its extension; Cursor only when authority exists at launch.
- `longhouse-server continue <session_id> <text>` (SEND) and `continue --steer`
  send with the owner's device credential, so the target reads it as the owner
  typing. Inside a managed session, `continue` to another target is refused unless
  `--as-owner` says the words are the owner's.

## Provider paths

### Claude (`longhouse claude`)

- Control is Claude's native channel/MCP/stdin path. Send, interrupt and
  active-turn steer are first-class. The Runtime Host gates `intent=steer` on a
  fresh active phase (`session.input.activity.read.v2`); otherwise it answers
  `409 steer_requires_active_turn` with `retry_with_intent: queue`.
- Owner sends arrive as the user's own words through the Longhouse Claude Code mod
  (`engine/assets/claude-mod`, `--plugin-dir`, Claude Code 2.1.287+), which submits
  with `$.prompt.submit({ asUser: true })`. The mod runs
  `longhouse-engine claude-owner-input watch`; the engine owns the queue and ledger
  in `~/.claude/channels/longhouse/sessions/<session>.owner-input/` (`route.json`
  = chosen path and why; `ledger.jsonl` = claimed, submitted, delivered). Peer
  envelopes, steers and pause answers never use this path. Without the mod, sends
  use the channel; every send result names the path (`owner_input_path`,
  `owner_input_reason`). A leading `/` is refused (`owner_input_leading_slash`).
- Never SIGINT a Claude Helm process: Claude Code treats it as shutdown. Interrupt
  with the mod calls `$.turn.abort` and terminates the foreground Bash tool's
  process group. Without it, interrupt writes `<session>.interrupt.json` beside the
  channel state, terminates that group, and the lifecycle hook returns
  `continue: false` until the turn ends. Terminate is SIGTERM then SIGKILL on the
  recorded pid.
- Helm is terminal-originated only; the Machine Agent has no remote Helm launch.
- The channel is a private MCP server passed with `--mcp-config`, so Claude's banner
  always prints `server:longhouse-channel · no MCP server configured with that
  name`. That is cosmetic. Prove liveness with
  `longhouse-engine claude-channel inspect --session-id <id>` and a real
  `claude-channel send`.
- Pass hook tokens through process env, never in shell commands or PTY launch logs.
- Liveness comes from process scanning
  (`local_health/process.py`, `_collect_managed_sessions_by_process`); no detached
  bridge daemon, state file or flock sidecar is required.
- Bedrock Claude needs a local binary patch for channels: `private-claude-bedrock-channels`
  (local-only skill, not in git).

### Codex (`longhouse codex`)

- Uses the user's stock `codex` from PATH. `--codex-bin` / `LONGHOUSE_CODEX_BIN`
  are debug overrides only.
- The Python CLI starts `longhouse-engine codex-bridge` and passes the device token
  through `LONGHOUSE_CODEX_BRIDGE_TOKEN`; bridge argv must never contain it.
- The bridge starts `codex app-server`, fronts it with `engine/src/codex_ws_relay.rs`,
  and attaches the TUI with `codex --enable tui_app_server --remote ...`.
- The coordination MCP is supplied by process-scoped `-c mcp_servers.longhouse.*`
  overrides. Launch must never edit the user's `~/.codex/config.toml`.
- Approval posture is `provider_local` (Codex owns its approval UI); the dangerous
  bypass flag records `bypass`. There is no `remote_approve` until a remote
  permission transport exists.
- State, logs, locks and sockets: `~/.longhouse/managed-local/codex-bridge/`.
  Hook scripts such as `longhouse-codex-hook.sh` are Longhouse's, not provider binaries.
- Helm is a long-running app-server with a visible TUI attached via `--remote`.
  Console (`codex_exec`) runs one turn, exits, and resumes durable state later.
  `launch_mode=detached_ui` is an internal no-attach posture, not a product mode or
  a remote Helm grant; never infer Helm provenance from it.
- A wrapper-observed `codex --remote` exit or terminal signal ends Helm ownership
  and stops the bridge/app-server. A missing TUI without that evidence (for example
  after SIGKILL) is degradation evidence, not permission to guess. Only an explicit
  no-attach launch keeps a background bridge.
- Do not ship a Codex runtime payload, install `longhouse-codex`, recreate
  `~/.longhouse/runtimes/codex`, download Codex release assets, or patch a fork.
  Install/repair code may conservatively remove old artifacts.

### OpenCode (`longhouse opencode`)

- Starts stock `opencode serve` on localhost through `opencode_server_bridge`, then
  attaches with stock `opencode attach`. Send, interrupt and terminate are Helm
  controls; steer is not advertised until a true mid-turn injection is proven.
- State lives under `~/.claude/managed-local/opencode-server/`, with the server
  password in a 0600 file. Hook tokens never go in the state file or server log.
- Launch is idempotent per session id: a retry reuses a live state file. An attached
  wrapper stops its `opencode serve` on any TUI exit or signal; no-attach launches
  stay until explicitly stopped.
- Advertise `opencode.*` control only when stock `opencode` is on PATH.

### Pi (`longhouse pi`)

- Launches stock `pi` in the real terminal with a process-scoped `-e` extension;
  nothing global is installed or rewritten. State: `~/.longhouse/managed-local/pi-helm/`.
- Channel authority is bound to launch, native session, connection and lease
  generation. Pi rewrites its process title, so judge liveness by recorded PID/birth
  identity and authenticated channel evidence, not argv.
- Idle send, boundary steer, follow-up, abort and process-group terminate are
  distinct. Quiescence is `agent_settled`, never `agent_end`.
- Cold resume verifies the retained native file/header and refuses a second owner.
  There is no live TUI attach.
- Console is `pi -p --mode json` with `provider_local` approvals. Native JSONL is
  durable history; live previews never create archive rows.

### OMP (`longhouse omp`)

- A distinct provider, not a Pi alias. OMP accepts Pi-style `PI_*` variables; keep
  them for the stock binary, but Longhouse's own discovery and launch roots use
  `LONGHOUSE_OMP_*` or an explicit `--session-dir`. Never dual-ingest a shared
  Pi/OMP archive.
- Helm is the stock TUI plus a launch-scoped extension and authenticated Unix
  channel. OMP's `agent_end` without `isTerminal`/`willContinue` is terminal;
  `willContinue: true` is continuation. Do not copy Pi's `agent_settled` semantics.
- Console is one `omp -p --mode json` process per turn, `provider_local`; no remote
  approval or auto-approval fallbacks.
- Console RPC stdin: write nothing until OMP prints its `ready` frame. Stock OMP's
  stdin reader wedges permanently if a byte is pending at startup and a large frame
  follows; the process stays idle and never acknowledges. Writes are stall-bounded
  and an unacknowledged prompt fails the turn after 30 s (`engine/src/console_rpc.rs`).
  Reproduce on the bench only.
- Bind exact native identity, workspace and source file before claiming ownership;
  cold Resume refuses a live prior owner.

### Cursor

- Helm is `cursor_helm` over stock `cursor-agent` (`LONGHOUSE_CURSOR_BIN` overrides
  the binary): the launcher owns the PTY, and the Machine Agent forwards send,
  interrupt, steer and terminate over a per-session Unix socket with a heartbeat
  lease. Console is `cursor_print`. `cursor_acp` and `cursor_exec` are read aliases
  for archived sessions only. Per-operation status: `cursor` cell in
  `schemas/managed_providers.yml`.

### Antigravity and future CLIs

- Longhouse owns the wrapper and control path; the provider CLI stays user-owned.
  Do not infer one provider's liveness or control model from another.
- Antigravity Console ownership comes from stock stream-json `init.conversation_id`,
  not result timing or log text. A pending identity must not let its transcript
  become an unrelated Shadow session.
- Its source is `.system_generated/logs/transcript_full.jsonl`. The shorter
  `transcript.jsonl` truncates tool output; never ingest both or prefer it.

## Workflows

### Version or binary drift

1. Compare `command -v <provider>` and `<provider> --version` with the process
   attached to the managed session.
2. For Codex, check whether `LONGHOUSE_CODEX_BIN` or `--codex-bin` forces a non-PATH binary.
3. Look for legacy artifacts: `~/.local/bin/longhouse-codex`, `~/.longhouse/runtimes/codex`.
   Remove them through the existing cleanup in `local_runtime_installer.py`, not by hand.
4. Provider release status and local live proof are separate feeds. Release-status
   warnings are advisory in local health; concrete proof failures and version-probe
   failures still warn; red release blockers stay hard.

### Codex bridge failures

1. Read `~/.codex/log/codex-tui.log`, the bridge `.json` state and `.log` under
   `~/.longhouse/managed-local/codex-bridge/`, and the thread's rollout JSONL
   (before trusting `last_turn_status`).
2. Check app-server `readyz` and whether the relay URL was logged.
3. For `longhouse-engine codex-bridge start` repros use `--isolation-root <tmp-dir>`
   (bridge state to `<tmp>/codex-bridge`, Longhouse state to `<tmp>/longhouse`). With
   lower-level flags set both `--state-root` and `--longhouse-home`; `--state-root`
   alone is rejected.
4. `No active thread is available.` during fresh remote-TUI startup is normal until
   `StartupThreadStarted`. Check state, thread id, readyz and rollout progress first.
5. Never kill by `ps | grep codex app-server`. Take `session_id`, `pid`,
   `app_server_pid`, `app_server_pgid` and `ws_url` from the state file and stop with
   `longhouse-engine codex-bridge stop --session-id ...`.

### Changing managed provider code

| Area | Where |
|---|---|
| Codex launcher (`launch_managed_codex`) | `engine/src/longhouse.rs` |
| Codex bridge, WS relay | `engine/src/codex_bridge.rs`, `engine/src/codex_ws_relay.rs` |
| Installer and cleanup | `server/zerg/services/local_runtime_installer.py` |
| Local health and liveness | `server/zerg/services/local_health/` (`classifier.py`, `process.py`, per-provider files) |
| Served state wording and facts | `session_state_contract.py`, `session_state_facts_projector.py` (`server/zerg/services/`) |
| Phase ledger | `engine/src/state/session_phase.rs` |

Tests: `make test-engine` for launcher, bridge and relay changes;
`cd server && uv run pytest tests_lite/test_local_runtime_installer.py` for installer
changes; `make dogfood-refresh` to install into this Mac's runtime (`HERE=1` for a
working tree).

## Naming

- `managed`: Longhouse-owned control path. `unmanaged`: imported or discovered, no
  live control. `Console`: product-owned, turn-scoped, no terminal. `one-shot` or
  `batch`: prompt-and-exit.
- `detached-ui` is an implementation name, not a mode.
- Keep `live`, `reattachable`, `phase-known` and `running` separate, and keep
  `control_path`, `liveness_model` and `state` separate in local-health JSON.
- Do not name constants or paths as if Longhouse owns a provider binary.

## Fallbacks

No hidden fallbacks. A necessary one is explicit in logs or UI, or behind a named
operator path, and never silently swaps a user-owned provider CLI for a
Longhouse-owned runtime.
