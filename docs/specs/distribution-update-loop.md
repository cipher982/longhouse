# Distribution And Update Loop

Status: Active Buildout
Last updated: 2026-04-07

## Goal

Make Longhouse installation, upgrade, and release behavior explicit across three different surfaces:

- hosted deploys on `zerg`
- the user-installed `longhouse` CLI package
- the separately installed runner binary/service

The product should never imply that a hosted deploy updates the local CLI, and the local CLI should not silently bypass the package manager that installed it.

## Vision

Longhouse should have one obvious zero-to-one local install story:

1. run the shell installer
2. finish guided onboarding
3. end up with one local runtime, not a bag of separate steps

For local users, "Longhouse installed" means:

- the `longhouse` CLI is on PATH
- the engine binary exists locally
- `connect --install` can lay down the engine service and hooks without extra artifact hunts
- macOS users can also get the ambient local-health menu bar from the same runtime lane

Homebrew remains a possible secondary channel later. It is not the primary product story now.

## Current State

- `scripts/install.sh` is the public bootstrap path.
- The installer currently ensures `uv`, ensures Python 3.12, then installs or upgrades `longhouse`.
- `.github/workflows/publish.yml` builds a wheel, uploads it to the GitHub release, and publishes the package to PyPI.
- `README.md` already documents `uv tool upgrade longhouse`.
- `longhouse doctor` is already the natural post-install/post-upgrade verification surface.

Current weakness:

- install and upgrade behavior are usable, but the product loop is not yet explicit about source-of-truth, install metadata, version checks, or when users need to rerun `longhouse connect --install`.

## Canonical Surfaces

### Hosted deploy

Updates:

- public demo runtime
- control plane
- hosted tenant runtimes

Does not update:

- the user's installed `longhouse` CLI
- the user's installed runner binary

### CLI/package release

Updates:

- `longhouse` on the user's machine
- CLI launch flows such as `longhouse claude`
- local hook wiring and machine-local service management behavior

Default stable source:

- PyPI package `longhouse`

User upgrade path:

- `uv tool upgrade longhouse`

### Runner release

Updates:

- the separately installed runner binary/service

This is a different release and update lane from the main `longhouse` CLI package.

## Product Rules

- Hosted deploy language must not be used for CLI-only changes.
- The CLI may check whether it is outdated, but it should not silently mutate itself on startup.
- The package manager that installed the CLI remains the source of truth for upgrades.
- Longhouse should record enough local install metadata to recommend the correct upgrade command later.
- Upgrades that affect local hooks or background services must explicitly call out the repair step instead of assuming the package update handled everything.
- The shell installer is the primary acquisition path until there is a fully bundled desktop app.
- `longhouse connect --install` is the canonical local-runtime install verb after the CLI is present.
- The local runtime must ship as explicit versioned artifacts; the installer may not assume repo-local builds.
- On macOS, the ambient menu bar helper belongs to the same local-runtime lane as the engine service, not a separate product path.

## Install Source Policy

### Stable MVP

- Canonical package source: PyPI
- Canonical package manager path: `uv tool install longhouse`
- Bootstrap convenience path: `curl -fsSL https://get.longhouse.ai/install.sh | bash`

The shell installer remains the thin bootstrap wrapper around the same package story users will use later for upgrades.

### Canonical local runtime

After the CLI is present, one command owns local runtime repair and reinstallation:

- `longhouse connect --install`

That command is responsible for:

- ensuring the engine binary exists locally
- installing the engine launchd/systemd service
- installing provider hooks
- installing the ambient macOS `Longhouse.app` helper when enabled

This keeps the local runtime install surface singular even if the shell installer, onboarding flow, and future bundled app all call into it.

### Deferred

- Homebrew tap
- preview channel
- signed release manifest with min-supported versions
- fully automatic runner and engine update coordination
- app-owned helper lifecycle through Apple-native service management

## Local Runtime Artifacts

The local runtime now has four artifact classes:

- Python wheel: `longhouse`
- engine binary: `longhouse-engine-<platform>`
- macOS ambient app bundle archive: `longhouse-local-health-app-darwin-arm64.zip`
- macOS debug window binary: `longhouse-local-health-window-darwin-arm64`

Artifact policy:

- the `vX.Y.Z` GitHub release is the versioned source of truth for local runtime binaries
- the CLI version and runtime artifact version should match the same tag by default
- local/dev validation may override artifact sources explicitly
- on macOS, `connect --install` should install the ambient helper as `~/Applications/Longhouse.app`

The shell installer should not know asset naming rules itself forever. The long-term goal is that the CLI/runtime layer owns artifact resolution while the shell script stays thin.

## Local Install Metadata

Store install metadata under:

- `~/.longhouse/install.json`

MVP fields:

```json
{
  "install_method": "uv",
  "install_source": "pypi",
  "package_name": "longhouse",
  "channel": "stable",
  "installed_version": "0.1.0",
  "installed_at": "2026-04-07T12:34:56Z",
  "last_upgrade_at": "2026-04-07T12:34:56Z"
}
```

Purpose:

- show the right upgrade command
- avoid guessing how the CLI was installed
- give `doctor` and future support flows concrete state

## Update Check

MVP behavior:

- explicit checks only
- no background startup check yet
- no silent self-update

Initial commands:

- `longhouse version --check`
- `longhouse upgrade`

Version source:

- PyPI JSON for the `longhouse` package

Later:

- cached background check for interactive commands
- release manifest served from Longhouse-controlled infrastructure
- min-supported version warnings

## Upgrade UX

### `longhouse version --check`

Reports:

- installed version
- latest available stable version
- whether an update is available
- recommended upgrade command based on install metadata

### `longhouse upgrade`

MVP behavior:

- support the current canonical install path: `uv`
- run `uv tool upgrade longhouse`
- refresh local install metadata
- tell the user when `longhouse connect --install` should be rerun

If install metadata is missing or unknown, degrade to a clear instruction instead of guessing aggressively.

## Post-Upgrade Repair

Package upgrade and local wiring refresh are not always the same thing.

For MVP:

- `longhouse upgrade` should end with a clear reminder to run `longhouse connect --install` when local launcher/hook behavior may have changed.
- `longhouse doctor` remains the verification command after install or upgrade.

Later:

- tie this to explicit version thresholds or a release manifest field such as `requires_connect_reinstall_since`.

## Testing Loop

The default test loop for installer and upgrade work should not use David's real machine state.

### Canonical local loop

Use a disposable `HOME` with the existing installer smoke harness:

- `scripts/ci/installer-first-run.sh`

This isolates:

- `~/.longhouse`
- `~/.local/bin`
- shell profile changes
- onboarding and `connect --install` side effects
- local runtime artifact installation

### MVP upgrade test

The smoke harness should support:

1. install from one package source/version into a temp `HOME`
2. verify CLI metadata and basic command health
3. point at a newer package source/version
4. run `longhouse upgrade`
5. verify the upgraded version and metadata without touching the real laptop

### Deferred environments

- Linux VM matrix for systemd behavior
- macOS VM/runner validation for launchd behavior
- Docker-based smoke for purely CLI/package flows where launchd/systemd are not required

Docker is acceptable for narrow package-install/update checks, but temp-home host execution should stay the main fast loop because Longhouse also needs real shell/profile/hook behavior.

## MVP Scope

Implement now:

- install metadata file
- PyPI-backed explicit version check
- `longhouse upgrade`
- installer writes metadata
- disposable temp-home upgrade smoke
- released engine binaries and macOS menu bar binaries
- `connect --install` as the singular local-runtime repair/install seam
- installer/onboarding language that describes one runtime, not a second hidden step

## Success Criteria

- New users have one obvious acquisition path: the shell installer.
- Onboarding plus `connect --install` produce one coherent local runtime instead of separate manual engine/menu-bar steps.
- The ambient macOS menu bar is installed from the same runtime lane as the engine service.
- The local installer smoke validates the real runtime path in a disposable `HOME`.
- Future pivot to a bundled macOS app remains a control-adapter swap, not a rewrite of health classification or UI state contracts.

Do not implement yet:

- silent auto-update on startup
- Homebrew distribution
- npm distribution
- multi-channel update policy
- release manifest service

## Definition Of Done

- users can install Longhouse through the bootstrap installer without touching repo internals
- users can ask the CLI whether they are outdated
- users can upgrade the CLI through a first-party `longhouse upgrade` command
- the CLI tells users when `longhouse connect --install` is still required
- install and upgrade can be exercised in a disposable sandbox without mutating David's real laptop environment
