# No-Python Device Cutover Execution

Status: Active

This is the execution companion to [Rust Edge Provider Parity](rust-edge-provider-parity.md). “Done” means a normal installed-device command uses compiled Longhouse binaries and stock user-installed providers; Runtime Host Python is explicitly out of scope.

## Current checkpoint

| Surface | State | Cutover gap |
|---|---|---|
| paired native installer | implemented | fresh-device install/repair proof |
| Codex Helm | implemented | doctor/provenance parity and hermetic provider proof |
| Claude Helm | implemented, not cutover-ready | fix review blockers: idempotent gate install, resume/contracts, unmanaged PID, safe hook command quoting |
| OpenCode Helm | control native; launch Python | native bridge start/attach/stop and runtime-plugin decision |
| Cursor Helm | control native; launch Python | native PTY launcher and recovery |
| Antigravity | Python wrapper/hook | native adapter or explicit product exclusion |
| local health/provider proof/repair | mixed Python | native command ownership and hermetic release gate |

## Delivery order

1. Close Claude’s four reviewed gaps and prove its native Helm lifecycle.
2. Implement `longhouse-engine opencode-bridge start|stop` and public native OpenCode launch/attach/stop. Preserve schema-v1 state, localhost-only health, process identity, bounded attached cleanup, and explicit detached survival. Do not emit coordination MCP or answerable permission pauses until their native counterparts exist.
3. Port Cursor Helm’s foreground process-group/control-socket owner to the facade.
4. Make the Antigravity include/exclude decision explicit. Inclusion requires a no-Python hook inbox plus real `agy` canary; exclusion removes it from the normal-device promise and capability advertising.
5. Replace the remaining normal-device Python commands: auth/connect/install/repair/local-health/provider-live/doctor, or split server-only Python behind the explicit `longhouse-python` command.
6. Add and pass the installed-artifact release gate, rebase, review, push, release, and dogfood-refresh.

## Non-negotiable contracts

- Provider binaries stay user-owned; Longhouse never downloads or forks them.
- Tokens travel only through environment or protected state, never argv/logs/receipt text.
- Attached Helm exit or terminal signal stops wrapper-owned provider infrastructure. Only explicit no-attach survives.
- State uses atomic private writes and strict process identity before destructive cleanup.
- Unsupported provider operations remain explicitly unsupported.
- Normal device execution must never invoke `python`, `python3`, `uv`, `pip`, or `longhouse-python`.

## Release evidence

The final hermetic test installs the paired artifacts into a fresh home with trap executables for `python`, `python3`, `uv`, `pip`, and `longhouse-python`. It proves install/repair, local health/provider proof, and each supported provider’s launch, attach/reattach, send, interrupt, stop, clean exit, and explicit detached behavior. Runtime Host may run outside the trapped device environment.

## Completion checklist

- [ ] Claude reviewed blockers resolved and provider proof complete.
- [ ] OpenCode native Helm complete and proof complete.
- [ ] Cursor native Helm complete and proof complete.
- [ ] Antigravity included natively or explicitly excluded.
- [ ] No normal device CLI route requires Python.
- [ ] Native local-health/provider-live/repair gate complete.
- [ ] Hermetic installed-artifact gate passes.
- [ ] Fresh Hatch Sol and Cursor reviews dispositioned.
- [ ] Branch rebased, pushed, released, and dogfood-refreshed.
