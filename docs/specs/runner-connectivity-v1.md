# Runner Connectivity V1

Status: In progress
Last updated: 2026-03-07

## Goal

Make Longhouse runner connectivity feel boringly reliable for pre-launch users while keeping the long-term product shape clean:
- **Runner-first** for the default hosted UX
- **SSH as an advanced connector**, not the foundation
- Clear install modes so Linux servers do not depend on login-session quirks

## Decision Summary

We are **not** pivoting to SSH-only.

We will ship:
1. **Native runner** as the default connection path for macOS and Linux
2. **Linux install modes** so users can choose the right service model for the machine
3. **SSH bridge / advanced SSH connector** later for power users and homelabs

## Why

### Why not SSH-first

SSH is great for power users, but it is a poor default product primitive for a hosted assistant that must work across laptops, home servers, VPSes, NAT, and mixed device types.

Problems with SSH-as-default:
- Requires inbound reachability, VPN, bastions, or tailnet assumptions
- Pushes key management and host verification into the core UX
- Makes per-device presence and status harder to model cleanly
- Weakens the product story for non-ops users
- Creates a more awkward hosted security model than outbound device connectors

### Why runner-first

Runner-first gives us:
- Outbound-only connectivity
- Better NAT/home-network behavior
- One device credential per machine
- Cleaner online/offline presence and audit trail
- Better cross-platform consistency

## Product Surface

### Connection modes

| Mode | Audience | Default | Notes |
| --- | --- | --- | --- |
| `native_runner` | Everyone | Yes | Primary product path |
| `ssh_bridge` | Power users | No | Future: one trusted runner fans out over SSH |
| `tailscale_ssh` | Advanced users | No | Future optional integration |

### Install modes

| OS | Mode | Service model | Intended machine |
| --- | --- | --- | --- |
| macOS | `desktop` | LaunchAgent | Personal laptop / desktop |
| Linux | `desktop` | `systemd --user` | Personal laptop / desktop |
| Linux | `server` | system service | Always-on server / VM / NAS |

## Phase 1 Scope (Current Work)

Ship the minimum set needed to make the runner UX sane on Linux without overbuilding:

1. Add a living design doc and track quirks here
2. Add explicit Linux install modes: `desktop` and `server`
3. Keep default install mode conservative (`desktop`) for now
4. Make `server` mode install a real system service that survives logout/reboot
5. Add backend tests for the served install script contract
6. Surface server mode in the current UX where practical without redesigning onboarding

## Phase 1 Non-Goals

- Full SSH connector implementation
- Tailscale integration
- Windows support
- Auto-updater / release channels
- Device approval UX for sensitive commands

## Phase 1 Contract

### Install script API

`GET /api/runners/install.sh`

Accepted inputs:
- `enroll_token` (required)
- `runner_name` (optional)
- `longhouse_url` (optional override)
- `mode` (optional, `desktop` or `server`)
- env override: `RUNNER_INSTALL_MODE=desktop|server`

Behavior:
- Default mode remains `desktop`
- On macOS, mode is currently advisory only; install remains LaunchAgent-based
- On Linux:
  - `desktop` => user service (`systemctl --user`)
  - `server` => system service (`systemctl`)

### Linux `server` mode

Implementation target:
- Binary at `/usr/local/bin/longhouse-runner`
- Env/config at `/etc/longhouse/runner.env`
- Unit at `/etc/systemd/system/longhouse-runner.service`
- Service runs as the installing user, not root, but starts via the system service manager
- Systemd provides env vars to the runner process directly; the runner should not need `--envfile` in `server` mode

### UX target

Users should not need to know what `loginctl enable-linger` means.

Instead, the product should express intent in user language:
- "Personal laptop / desktop"
- "Always-on server / VM"

## Future Phases

### Phase 2
- Add richer runner health reasons in the UI
- Add repair/reinstall actions
- Add version drift visibility

### Phase 3
- Add `ssh_bridge` mode
- One trusted runner can execute against downstream SSH hosts
- Treat SSH as advanced / power-user setup

## Open Questions

- Should Linux `auto` mode exist, or should the UI always force an explicit machine type choice?
- Should `server` mode eventually create a dedicated `longhouse-runner` OS user instead of using the installing user?
- Should chat/Oikos runner setup cards expose `desktop` vs `server` immediately, or after the install path is stable?

## Progress Log

- 2026-03-07: Created initial spec. Decision is runner-first, SSH-optional.
- 2026-03-07: Implemented `RUNNER_INSTALL_MODE=desktop|server` in the live install script and activated the `mode` query param on `GET /api/runners/install.sh`.
- 2026-03-07: Linux `server` mode now installs a systemd system service with `EnvironmentFile=/etc/longhouse/runner.env`, while `desktop` keeps the existing `systemd --user` path.
- 2026-03-07: Added backend tests for the served install script contract and validated the generated shell with `bash -n`.
- 2026-03-07: Updated the current UX (`AddRunnerModal`, `RunnerSetupCard`) so users can choose **Desktop / Laptop** vs **Always-on Linux Server** without needing to know `loginctl`.
- 2026-03-07: Removed the stale `apps/runner/scripts/install-linux.sh` helper because it was unreferenced and still encoded the old linger-dependent Linux install path.

## Discoveries / Quirks

- The live install script is served from `apps/zerg/backend/zerg/routers/templates/install.sh`, not from `apps/runner/scripts/install.sh`.
- The Linux installer currently uses a `systemd --user` service and explicitly warns that it only runs while the user is logged in.
- The runner binary can read environment variables directly; `server` mode can rely on a systemd `EnvironmentFile=` instead of forcing `--envfile`.
- The UI can synthesize a `server` install command client-side from `enroll_token` + `longhouse_url`, so we do not need an API schema change just to expose the new mode.
- `apps/runner/scripts/install-linux.sh` was unreferenced dead weight, so removing it is safer than pretending it is a maintained install path.
