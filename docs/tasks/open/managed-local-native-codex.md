# Managed-Local Native Codex

Status: In progress
Spec: `docs/specs/managed-local-native-codex-demo.md`
Last updated: 2026-03-27

## Goal

Ship a managed-local Codex experience where Longhouse is operationally present but interactively invisible:

- the user stays on native Codex UI
- Longhouse observes and controls the same session out-of-band
- away-mode can continue that exact thread without cloud takeover
- tmux is no longer in the human input/render path

## Done when

- `longhouse codex` has a native interactive path where the user-facing UI is stock Codex, not Codex-inside-tmux.
- Longhouse can start, resume, steer, interrupt, read, and list managed Codex threads through a structured transport.
- Managed sessions emit both app-server notifications and Longhouse hook presence/transcript events.
- Approval requests from Codex are handled by Longhouse instead of crashing or forcing `approvalPolicy = never`.
- Longhouse can detach from a managed Codex session, continue it in away-mode, and later reattach with coherent history.
- Managed Codex sessions remain visible in native Codex history and remain correlated to Longhouse session IDs.
- The current tmux-backed managed-local path remains available as an explicit fallback until parity is proven.
- A real laptop dogfood flow passes: start locally, leave, continue from phone, return locally, same thread.

## Success criteria

- UI parity: no tmux in the interactive PTY path for the native managed flow.
- Control parity: no fake typing is required for the native managed flow.
- Data parity: Longhouse can reconstruct the managed thread from protocol events plus durable session state.
- Safety parity: approvals, interrupts, and failure states are explicit and recoverable.
- Operational parity: fallback tmux path still works during rollout.

## Checklist

- [x] Prove app-server can start/resume/steer/interrupt real Codex threads.
- [x] Prove hooks can coexist with app-server when `CODEX_HOME` and `codex_hooks` are configured correctly.
- [x] Prove `thread/read` reconstructs managed-thread history cleanly.
- [x] Prove `thread/list` can discover Longhouse-managed sessions reliably.
- [x] Implement and unit-test server-initiated app-server request handling in the canary, starting with approvals.
- [x] Prove a live approval-request round-trip against the real Codex binary.
- [x] Define the managed-session transport interface in Longhouse (`tmux_legacy` vs `codex_app_server`).
- [x] Decide the demo-path control bridge for `codex_app_server`: use a local Rust sidecar/daemon, not the current one-shot runner protocol.
- [x] Prove the dual-client topology: stock Codex TUI via `--remote` plus a second Longhouse client attached to the same local app-server/thread.
- [x] Build a Rust `longhouse-engine codex-bridge` MVP that owns app-server lifecycle, session/thread correlation, and approval handling.
- [x] Build an experimental backend live-sync/control path for app-server-managed Codex sessions.
- [x] Correlate Longhouse session IDs with Codex thread IDs durably.
- [ ] Add a real managed-local native-Codex dogfood path behind a feature flag.
- [ ] Route Loop continue/reply/interrupt actions through the native Codex bridge.
- [ ] Keep tmux as an explicit fallback path during rollout.

## Notes

- Current canary commits: `6d9c3988`, `72d968ed`, `00a7420d`.
- The managed-local transport seam now lives in `server/zerg/services/managed_local_transport.py`; tmux remains the only implemented runtime path, and `codex_app_server` is now an explicit reserved transport instead of an implicit future idea.
- The invalid earlier conclusion was "hooks do not fire under app-server." The corrected finding is that the canary had the wrong env/feature setup.
- Current critical risk is approvals and other server-initiated requests, not hooks.
- Live validation on 2026-03-27 proved hooks, `thread/read`, and `thread/list` when `sourceKinds` includes both `appServer` and `custom`.
- Live validation on 2026-03-27 also showed that workspace file writes surface as `fileChange` items/diffs without necessarily producing an approval request, and direct `command/exec` did not trigger an approval request in the basic `/bin/pwd` probe.
- Live validation on 2026-03-27 proved a deterministic real-binary approval round-trip with `approvalPolicy=on-request`, `sandbox=read-only`, and app-server feature flags `exec_permission_approvals` + `request_permissions_tool`. Prompting Codex to call `request_permissions` yielded `item/permissions/requestApproval`, which the canary auto-approved successfully.
- Current runner transport is not a drop-in app-server bridge: `runner_job_dispatcher` is one-shot and single-active-job-per-runner, while `runner/src/protocol.ts` only supports `exec_request` / `exec_cancel` plus stdout/stderr chunks. There is no persistent stdin channel or process session handle today.
- Architecture decision for the demo path: stop trying to make tmux invisible. Use a local Rust bridge that owns `codex app-server`, let stock Codex connect through `--remote`, and keep tmux only as fallback.
- Live validation on 2026-03-27 proved the stronger bridge-first topology on the real Codex binary: the observer can `thread/start`, launch stock Codex as `codex resume <thread_id> --enable tui_app_server --remote ws://127.0.0.1:<port>`, and then drive a successful `turn/start` on that same thread while the remote TUI stays alive.
- Live validation on 2026-03-27 proved the backend path with a real local dev server: a bridge-backed session posted runtime events to `/api/agents/runtime/events/batch`, shipped transcript turns through the existing Codex shipper, replaced the placeholder `provider_session_id` with the real Codex `thread_id`, and accepted a second `codex-bridge send` on the same Longhouse session/thread.
- One bridge edge is now understood: a freshly started zero-turn thread has no rollout file yet, so second-client control must target the already-loaded thread directly with `turn/start` instead of trying `thread/resume` first.
- The installed Codex build on this machine still needs `--enable tui_app_server` for `--remote`; the flag exists in help text but the feature is not effectively on by default.
- For demo/history compatibility, `codex app-server` should run with `--session-source cli` and Longhouse should rely on explicit `thread_id` mapping rather than a custom session source.
- The canary `thread/list` probe must include `cli` and `vscode` alongside `appServer` and `custom` when validating sessions launched with `session_source=cli`.
- One remaining canary caveat: the remote TUI bootstrap succeeded reliably against the real HOME, but the isolated-home variant failed on this machine with `account/rateLimits/read failed during TUI bootstrap`. That is a canary/home-isolation issue, not evidence that the real native topology is broken.
