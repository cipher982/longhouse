# No-Python Device Cutover Execution

Status: Active

This is the execution companion to [Rust Edge Provider Parity](rust-edge-provider-parity.md). “Done” means a normal installed-device command uses compiled Longhouse binaries and stock user-installed providers; Runtime Host Python is explicitly out of scope.

## Current checkpoint

| Surface | State | Cutover gap |
|---|---|---|
| paired native installer | implemented | fresh-device install/repair proof |
| Codex Helm | native launch/attach/stop implemented | hermetic provider proof; doctor/provenance is not cutover-critical |
| Claude Helm | native launch/configure implemented | native resume/contract parity and reviewed hook fixes; prove installed lifecycle |
| OpenCode Helm | native facade and localhost bridge implemented | independent lifecycle review, installed-provider proof; runtime plugin remains deliberately absent until native permission reply exists |
| Cursor Helm | excluded from native normal-device release | reintroduce only as one native PTY/control/permission/transcript runtime |
| Antigravity | excluded from native normal-device release | reintroduce only with a native hook inbox and real `agy` canary |
| public device CLI | tiny native facade; Python owns auth/connect/repair/status | explicit public-command matrix and native ownership |
| desktop/menu bar | invokes `longhouse local-health --fast --json` | preserve that native public contract |

## Delivery order

1. Publish the public `longhouse` command matrix: each current Python device command is either ported, explicitly retained only under `longhouse-python`, or removed. Auth, `connect --install`, repair, and `local-health --fast --json` are critical-path commands because provider Helm and the Desktop depend on them. Update `config/native_device_entrypoints.json` to reflect real facade/engine targets.
2. Grow the hermetic installed-artifact test now, beginning with install/repair and native `local-health --fast --json`; trap `python`, `python3`, `uv`, `pip`, and `longhouse-python`. Each later provider cutover extends this same gate.
3. Prove the new `longhouse-engine opencode-bridge` plus public native OpenCode launch/attach/stop on installed artifacts. Preserve schema-v1 state, localhost-only health, process identity, bounded attached cleanup, and explicit detached survival. Do not emit coordination MCP or answerable permission pauses until their native counterparts exist.
4. Cursor Helm is excluded from the native normal-device release. Reintroduce it only as one native PTY/control/permission/transcript runtime; do not leave a Python launch path advertised as supported.
5. Close Claude’s reviewed gaps and prove its installed lifecycle. This is important parity work, but does not block the other provider ports.
6. Antigravity is excluded from the native normal-device release. Remove its capability advertising; an affirmative future inclusion requires a no-Python hook inbox and real `agy` canary.
7. Remove default-PATH/installer `uv` device ownership, retain any server-only compatibility surface behind explicit `longhouse-python`, then rebase, review, push, release, and dogfood-refresh.

## Non-negotiable contracts

- Provider binaries stay user-owned; Longhouse never downloads or forks them.
- Tokens travel only through environment or protected state, never argv/logs/receipt text.
- Attached Helm exit or terminal signal stops wrapper-owned provider infrastructure. Only explicit no-attach survives.
- State uses atomic private writes and strict process identity before destructive cleanup.
- Unsupported provider operations remain explicitly unsupported.
- Normal device execution must never invoke `python`, `python3`, `uv`, `pip`, or `longhouse-python`.

## Release evidence

The evolving hermetic test installs the paired artifacts into a fresh home with trap executables for `python`, `python3`, `uv`, `pip`, and `longhouse-python`. It first proves install/repair and the Desktop-facing `longhouse local-health --fast --json` contract, then adds each supported provider’s launch, attach/reattach, send, interrupt, stop, clean exit, and explicit detached behavior. It also proves installers no longer re-stamp Python hooks. Runtime Host may run outside the trapped device environment.

## Completion checklist

- [ ] Public command matrix complete; `native_device_entrypoints.json` matches shipped ownership.
- [ ] Native auth/connect/repair and Desktop-facing local-health contract complete.
- [ ] Claude reviewed blockers resolved and provider proof complete.
- [ ] OpenCode native Helm complete and proof complete.
- [x] Cursor Helm explicitly excluded from the native normal-device release.
- [x] Antigravity explicitly excluded from the native normal-device release.
- [ ] No normal device CLI route requires Python.
- [ ] Default device install/PATH never selects Python; server compatibility, if retained, is only `longhouse-python`.
- [ ] Hermetic installed-artifact gate passes.
- [ ] Fresh Hatch Sol and Cursor reviews dispositioned.
- [ ] Branch rebased, pushed, released, and dogfood-refreshed.
