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

## Solo-Dev Validation Plan

### Principles
- Keep the matrix intentionally small and risk-based; cover one representative machine per failure class instead of chasing every distro/browser combination.
- Prefer deterministic automation plus rich failure artifacts over ad-hoc manual poking.
- Use your own machines as the first canary ring; use cloud real-device services only where emulation or hosted CI cannot answer the question.
- Keep hosted macOS coverage selective because private-repo macOS GitHub Actions minutes are materially more expensive than Linux.

### Validation Rings

1. **Contract ring**
   - Keep fast tests around install-script generation, runner tool contracts, and Oikos runner availability behavior.
   - Every installer change must still prove the served shell parses and that mode-specific output stays correct.
2. **Browser onboarding ring**
   - Use Playwright projects for Chromium, Firefox, WebKit, plus one mobile Safari and one mobile Chrome emulation profile.
   - Reuse dedicated test accounts/auth state where it is safe, but keep at least one fresh-account onboarding path for the real first-run experience.
   - Capture traces on retry plus HTML report/log artifacts for CI debugging.
3. **Hosted CI OS ring**
   - Run a narrow GitHub Actions matrix on `ubuntu-24.04`, `ubuntu-24.04-arm`, and `macos-latest`.
   - Use this ring for installer fetch/parse, non-interactive setup, and API/UI smoke checks.
4. **Self-hosted hardware ring**
   - Attach a tiny labeled fleet of real machines to GitHub Actions for cases emulation cannot prove: real `systemd`, real `launchd`, reboot survival, home-LAN quirks, and long-lived service behavior.
   - Current pre-launch canaries can simply be David-owned hardware: local macOS arm64, `clifford` (Linux x64), and `cube` (Linux x64).
   - For Linux reboot/persistence proof, prefer disposable Ubuntu cloud-image VMs on `cube` over rebooting shared long-lived hosts.
5. **Real-device spot-check ring**
   - Before shipping onboarding UI changes, do short manual sessions on a real iPhone Safari device and a real Android Chrome device via BrowserStack Live or AWS Device Farm remote access.
   - Use BrowserStack Local or an equivalent secure tunnel when validating localhost/staging builds that are not publicly reachable.
6. **Nightly synthetic first-user ring**
   - Scheduled workflow provisions or resets a fresh dev instance, walks the Add Runner flow, installs a runner on a disposable target, waits for heartbeat, runs `runner_exec hostname`, then tears down or repairs.
   - `workflow_dispatch` should trigger the same suite on demand before releases.

### Recommended Pre-Launch Matrix

- **Browsers (automated):** Chromium, Firefox, WebKit, Mobile Safari emulation, Mobile Chrome emulation
- **Real machines (automated/self-hosted):** macOS arm64 laptop/desktop, Linux x64 always-on server, disposable Linux x64 VM on `cube`
- **Real devices (manual/cloud):** one current iPhone Safari session, one recent Android Chrome session
- **Deferred:** Windows, exhaustive distro coverage, broad browser/version cartesian products

### Exit Criteria

- Every onboarding change: browser onboarding ring green.
- Every runner install change: contract ring plus hosted CI OS ring green.
- Every release-candidate onboarding change: self-hosted hardware smoke plus real-device spot checks complete.
- Before inviting outside testers: nightly synthetic first-user ring green for at least several consecutive days.

## Open Questions

- Should Linux `auto` mode exist, or should the UI always force an explicit machine type choice?
- Should `server` mode eventually create a dedicated `longhouse-runner` OS user instead of using the installing user?
- Should chat/Oikos runner setup cards expose `desktop` vs `server` immediately, or after the install path is stable?
- Which real-device vendor should we standardize on for pre-launch spot checks: BrowserStack, AWS Device Farm, or whatever is already cheapest/easiest to wire into the current stack?

## Progress Log

- 2026-03-07: Created initial spec. Decision is runner-first, SSH-optional.
- 2026-03-07: Implemented `RUNNER_INSTALL_MODE=desktop|server` in the live install script and activated the `mode` query param on `GET /api/runners/install.sh`.
- 2026-03-07: Linux `server` mode now installs a systemd system service with `EnvironmentFile=/etc/longhouse/runner.env`, while `desktop` keeps the existing `systemd --user` path.
- 2026-03-07: Added backend tests for the served install script contract and validated the generated shell with `bash -n`.
- 2026-03-07: Updated the current UX (`AddRunnerModal`, `RunnerSetupCard`) so users can choose **Desktop / Laptop** vs **Always-on Linux Server** without needing to know `loginctl`.
- 2026-03-07: Removed the stale `apps/runner/scripts/install-linux.sh` helper because it was unreferenced and still encoded the old linger-dependent Linux install path.
- 2026-03-07: Researched a solo-dev validation strategy and added a layered plan: Playwright browser projects, hosted CI OS matrix, self-hosted canary hardware, and tiny real-device spot checks.
- 2026-03-07: Implemented the browser ring locally: onboarding config now covers Chromium, Firefox, WebKit, and mobile emulation, and a new runner setup smoke asserts desktop/server command generation on `/runners`.
- 2026-03-07: Added `make test-e2e-onboarding`, a dedicated GitHub Actions workflow for hosted + self-hosted onboarding validation, and a release-candidate checklist for real-device spot checks.
- 2026-03-07: The first live GitHub Actions run exposed a real workflow-shaping constraint: `jobs.<job_id>.if` is evaluated before `strategy.matrix`, so hosted baseline and extended coverage must be separate jobs rather than one matrix with `matrix.run_on_push` gating.
- 2026-03-07: Fixed `make onboarding-funnel` so the README contract now exercises the onboarding Playwright smoke instead of stopping at `/api/health`.
- 2026-03-08: Hosted onboarding/browser failures traced back to `POST /api/runners/enroll-token` returning 500 when `APP_PUBLIC_URL` was unset. Local/demo enrollment now derives `longhouse_url` from `request.base_url`, and the route has regression coverage.
- 2026-03-08: The remaining `contract-first-ci` fresh-clone smoke failure was a workflow mismatch, not another product bug: the job installed only Chromium but still ran the full onboarding Playwright project set. It now pins `ONBOARDING_PLAYWRIGHT_PROJECT=onboarding-chromium` to match the lightweight smoke contract.
- 2026-03-08: Real hardware validation uncovered a capability-preservation bug: the installer wrote `LONGHOUSE_URL`, `RUNNER_NAME`, and `RUNNER_SECRET`, but not `RUNNER_CAPABILITIES`. Re-enrolling an existing `exec.full` runner would therefore reconnect as the client default `exec.readonly`. The register response now returns a capabilities CSV and the installers persist it into every env file path.
- 2026-03-08: Live migration validation completed on owned hardware: `cinder` moved from a source-based LaunchAgent to the shipped binary LaunchAgent, and `clifford` moved from the old linger-dependent user service to the new Linux system service. Hosted Oikos successfully ran `hostname -s` against both immediately after install and again after service restarts.
- 2026-03-08: Implemented a disposable Linux VM canary with `scripts/runner-vm-canary.sh` and `scripts/runner-vm-canary-host.sh`. Live validation on `cube` passed end-to-end: Ubuntu `noble` cloud image -> `server` install -> guest reboot -> hosted runner online -> Oikos `runner_exec hostname -s` -> runner revoke -> VM destroy.

## Discoveries / Quirks

- The live install script is served from `apps/zerg/backend/zerg/routers/templates/install.sh`, not from `apps/runner/scripts/install.sh`.
- The Linux installer currently uses a `systemd --user` service and explicitly warns that it only runs while the user is logged in.
- The runner binary can read environment variables directly; `server` mode can rely on a systemd `EnvironmentFile=` instead of forcing `--envfile`.
- The UI can synthesize a `server` install command client-side from `enroll_token` + `longhouse_url`, so we do not need an API schema change just to expose the new mode.
- `apps/runner/scripts/install-linux.sh` was unreferenced dead weight, so removing it is safer than pretending it is a maintained install path.
- The onboarding Playwright config already existed, but it only covered Chromium; expanding the ring was a config/problem-shaping task, not a greenfield test harness.
- Cross-browser onboarding validation still depends on Playwright browser binaries being installed on the host; CI/workflows need to provision them explicitly.
- The current GitHub API token available in this shell did not expose repo-level self-hosted runner metadata, so optional Linux x64/macOS hardware jobs are wired with explicit labels rather than auto-discovery.
- `make onboarding-funnel` was previously only a server/health smoke; the browser step had to be added back into the README contract to make the synthetic first-user ring meaningful.
- GitHub Actions evaluates `jobs.<job_id>.if` before matrix expansion, so event gating that depends on `matrix.*` has to be expressed as separate jobs instead of a single conditional matrix job.
- Self-hosted `cube` workflows cannot assume `make` is already present; CI jobs that shell out through `make` need to install build tools explicitly.
- README test workflows also need `uv` bootstrapped explicitly on self-hosted runners because the harness shells out through `uv venv` and `uv pip`.
- Fresh-clone README smoke coverage also needs Node/Bun and a frontend build because the backend editable install force-includes `apps/zerg/frontend-web/dist`.
- Runner README smoke exposed a real packaging dependency gap: `apps/runner` needs `bun-types` in `devDependencies` because its `tsconfig.json` includes that type library.
- Once the README smoke became a true fresh-clone flow, its timeout needed to account for frontend asset build time on slower self-hosted machines like `cube`.
- The Add Runner modal is only as healthy as `POST /api/runners/enroll-token`; backend URL-resolution bugs there can masquerade as multi-browser UI failures even when selectors and rendering are fine.
- Existing self-hosted runners may already have higher capabilities than the backend default. The installer must persist server-provided capabilities on re-enroll or it will silently downgrade a working runner during migration.
- README/service smoke checks should poll health instead of sleeping a fixed number of seconds; cold-start variance already exceeds 4 seconds on a fresh local boot.
- Live checks showed `cube` is `x86_64`, not ARM, so disposable runner VMs there must use Ubuntu `amd64` cloud images.
- `cube` mounts both `/tmp` and `/var/tmp` as 2 GiB tmpfs; `uvtool` image sync needs a disk-backed `TMPDIR`, so the VM harness uses `/var/lib/longhouse-vm/tmp`.
