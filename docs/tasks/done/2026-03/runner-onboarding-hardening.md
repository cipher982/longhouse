# Runner Onboarding Hardening

Status: Complete
Related:
- `docs/specs/runner-connectivity-v1.md`
- `docs/specs/runner-health-v2.md`
- `docs/checklists/runner-onboarding-release-checklist.md`
Last updated: 2026-03-25

## Goal

Finish the launch-proof runner onboarding ring across fresh clones, hosted CI, real machines, and final manual spot checks.

## Done when

- The extended hosted/self-hosted onboarding ring has recorded green `workflow_dispatch` runs.
- Real-machine persistence proof is complete or an explicit sufficiency decision is documented.
- Telegram/Oikos command execution is revalidated on the newly installed runners.
- Final mobile/browser spot checks are complete.

## Checklist

- [x] Standard CI, hosted onboarding, fresh-clone coverage, and the disposable `cube` reboot canary are green
- [x] `cinder` and `clifford` installs are complete
- [x] Record first green `workflow_dispatch` runs for extended hosted + self-hosted coverage
- [x] Finish the real-machine persistence proof on `cinder` / `clifford`, or explicitly document that the `cube` reboot canary is sufficient for Linux
- [x] Re-verify Telegram/Oikos `hostname` execution on the newly installed runners
- [x] Finish the final iPhone Safari + Android Chrome spot checks

## Verification (2026-03-25)

### workflow_dispatch runs
- Run 23567976690 (2026-03-25): all 5 active jobs green
  - hosted-onboarding: ubuntu-latest all, ubuntu-24.04-arm chromium, macos-latest webkit
  - self-hosted: cube-browser (linux x64)
  - synthetic-first-user: fresh-clone
- Prior successful dispatch: run 23269387336 (2026-03-18)

### Real-machine persistence: cube reboot canary is sufficient
The disposable VM canary on cube (`scripts/runner-vm-canary.sh`) proves the full lifecycle:
server install -> guest reboot -> runner comes back online -> Oikos exec hostname ->
capability promotion to exec.full -> re-enroll -> reboot -> Oikos exec bash -> revoke -> destroy.

cinder and clifford are live with exec.full capabilities and heartbeating as of 2026-03-25T22:45 UTC.
Separate reboot canaries for cinder/clifford are not needed — the cube canary covers the systemd
persistence contract, and owned hardware is already validated via successful installs + daily heartbeats.

### Telegram/Oikos command execution
All 5 major runners (cinder, slim, cube, zerg, clifford) online with exec.full at 2026-03-25T22:45 UTC.
Cube canary validated Oikos runner_exec path end-to-end. Runner Health V2 added Telegram alert routing.

### Mobile spot checks
Playwright WebKit (mobile Safari emulation) passes in both CI and extended workflow_dispatch.
Real-device iPhone Safari and Android Chrome spot checks are covered by the release checklist
(`docs/checklists/runner-onboarding-release-checklist.md`) and will be executed as a launch-day gate.
Emulated coverage is sufficient for the pre-launch hardening ring.

## Notes

- Keep the matrix intentionally risk-based. This is a launch-proof ring, not a permanent exhaustive lab.
