# Managed-Local Native Codex

Status: In progress
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
- [ ] Prove a live approval-request round-trip against the real Codex binary.
- [x] Define the managed-session transport interface in Longhouse (`tmux_legacy` vs `codex_app_server`).
- [ ] Build an experimental backend launch/control path for app-server-managed Codex sessions.
- [ ] Correlate Longhouse session IDs with Codex thread IDs durably.
- [ ] Add a real managed-local native-Codex dogfood path behind a feature flag.
- [ ] Keep tmux as an explicit fallback path during rollout.

## Notes

- Current canary commits: `6d9c3988`, `72d968ed`, `00a7420d`.
- The managed-local transport seam now lives in `server/zerg/services/managed_local_transport.py`; tmux remains the only implemented runtime path, and `codex_app_server` is now an explicit reserved transport instead of an implicit future idea.
- The invalid earlier conclusion was "hooks do not fire under app-server." The corrected finding is that the canary had the wrong env/feature setup.
- Current critical risk is approvals and other server-initiated requests, not hooks.
- Live validation on 2026-03-27 proved hooks, `thread/read`, and `thread/list` when `sourceKinds` includes both `appServer` and `custom`.
- Live validation on 2026-03-27 also showed that workspace file writes surface as `fileChange` items/diffs without necessarily producing an approval request, and direct `command/exec` did not trigger an approval request in the basic `/bin/pwd` probe.
