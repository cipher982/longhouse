# Runner Onboarding Hardening

Status: In progress
Related:
- `docs/specs/runner-connectivity-v1.md`
- `docs/specs/runner-health-v2.md`
- `docs/checklists/runner-onboarding-release-checklist.md`
Last updated: 2026-03-17

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
- [ ] Record first green `workflow_dispatch` runs for extended hosted + self-hosted coverage
- [ ] Finish the real-machine persistence proof on `cinder` / `clifford`, or explicitly document that the `cube` reboot canary is sufficient for Linux
- [ ] Re-verify Telegram/Oikos `hostname` execution on the newly installed runners
- [ ] Finish the final iPhone Safari + Android Chrome spot checks

## Notes

- Keep the matrix intentionally risk-based. This is a launch-proof ring, not a permanent exhaustive lab.
