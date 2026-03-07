# Runner Onboarding Release Checklist

Use this before shipping runner onboarding changes that affect install commands, runner setup UI, or machine-type selection.

## Automated Gates

- [ ] `make onboarding-sqlite`
- [ ] `make test-e2e-onboarding PROJECT=onboarding-chromium`
- [ ] `make test-e2e-onboarding PROJECT=onboarding-webkit`
- [ ] `make test-e2e-onboarding PROJECT=onboarding-mobile-safari`
- [ ] Hosted workflow `Runner Onboarding Validation Ring` is green

## iPhone Safari Spot Check

- [ ] Open the target instance in real iPhone Safari or a cloud device lab session
- [ ] Navigate to `/runners`
- [ ] Open `Add Runner`
- [ ] Verify **Desktop / Laptop** command omits `RUNNER_INSTALL_MODE=server`
- [ ] Verify **Always-on Linux Server** command includes `RUNNER_INSTALL_MODE=server`
- [ ] Verify the command remains copyable and readable without horizontal clipping bugs
- [ ] Capture one screenshot if layout or copy behavior looks suspicious

## Android Chrome Spot Check

- [ ] Open the target instance in real Android Chrome or a cloud device lab session
- [ ] Navigate to `/runners`
- [ ] Open `Add Runner`
- [ ] Verify machine-type toggles remain tappable and visible
- [ ] Verify the generated command switches correctly between desktop and server modes
- [ ] Verify the modal can still be dismissed cleanly after copy/toggle interactions

## Runner Install Reality Check

- [ ] Paste the desktop command on a personal macOS/Linux machine and confirm the runner appears online
- [ ] Paste the server command on an always-on Linux machine and confirm the runner survives a service restart
- [ ] From Oikos or the runners page, verify a simple `hostname` command returns from the new runner
- [ ] Uninstall or revoke the temporary runner after the check completes

## Notes

- Prefer BrowserStack Live or AWS Device Farm Remote Access when you need a real device you do not own.
- Keep the manual session short: the goal is tap/layout/copy confidence, not exhaustive exploratory QA.
- If a self-hosted GitHub Actions runner exists for the target hardware, run the matching workflow job first and only do the manual device pass as the final release-candidate check.
