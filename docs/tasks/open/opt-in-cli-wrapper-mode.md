# Opt-In CLI Wrapper Mode

Status: In progress
Spec: `docs/specs/opt-in-cli-wrapper-mode.md`
Last updated: 2026-04-02

## Goal

Make wrapper mode an explicit, safe activation feature:

- default install stays non-invasive
- wrappers are enabled only by user choice
- install, onboarding, README, and docs all point to the same `longhouse wrap --install` path

## Done when

- The installer no longer auto-installs the old Claude shim path.
- `longhouse wrap` is the only public wrapper/install path we point users to.
- Onboarding and docs present wrappers as optional default-launcher behavior.
- Public CLI wording stops leaking `managed-local` where users see it.
- Wrapper/onboarding tests pass.

## Checklist

- [x] Remove legacy installer-side shim install behavior
- [x] Remove or deprecate the old shim artifact from the repo
- [x] Add active-task tracking and doc links for wrapper mode
- [x] Update CLI help / launch copy to use Longhouse-session wording
- [x] Update onboarding output to mention wrapper mode as optional
- [x] Update README / docs to explain the opt-in wrapper flow
- [x] Run backend wrapper/onboarding tests and frontend docs checks
- [x] Add an interactive onboarding opt-in for wrapper install without changing quick/headless defaults
- [ ] Decide whether `longhouse wrap` needs a stronger machine-readable status surface

## Notes

- Wrapper mode is an activation accelerator, not a launch prerequisite.
- The public story stays: imported sessions first, controllable Longhouse sessions second.
- First slice landed:
  - installer no longer auto-installs the old Claude shim path
  - wrapper mode is documented as `longhouse wrap --install`
  - public CLI wording now says "Longhouse session" instead of leaking `managed-local`
- Second slice landed:
  - interactive onboarding now offers wrapper install explicitly
  - `--quick` and headless onboarding still skip wrapper install by default
- Validation:
  - `make test`
  - `make test-frontend`
  - `make onboarding-sqlite`
  - `make test-install-first-run`
