# Distribution And Update Loop

Status: Active Buildout
Last updated: 2026-04-13

## Goal

Make Longhouse installation, upgrade, and release behavior explicit across four different surfaces:

- hosted deploys on `zerg`
- the human-facing macOS app lane
- the user-installed `longhouse` CLI package
- the separately installed runner binary/service

The product should never imply that a hosted deploy updates the local CLI, and the local CLI should not silently bypass the package manager that installed it.

## Vision

Longhouse should have one obvious install story per audience, not one transport forever:

1. macOS humans open `Longhouse.app`
2. agents, Linux users, and power users use the CLI installer lanes
3. both end up with one coherent local runtime, not a bag of separate steps

For local users, "Longhouse installed" means:

- the `longhouse` CLI is on PATH
- the engine binary exists locally
- `connect --install` can repair the engine service and hooks without extra artifact hunts
- macOS users also have a real `Longhouse.app` product surface, not just a helper binary on disk

The launch product decision for macOS lives in `docs/specs/macos-launch-product-shape.md`.
The app-first onboarding and naming cleanup plan lives in `docs/specs/local-app-product-unification.md`.

## Current State

- `scripts/install.sh` is the public bootstrap path.
- `get.longhouse.ai/install.sh` currently redirects to raw GitHub `main`, so remote installer canaries validate the published default-branch script, not unpushed local commits.
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
- `Longhouse.app` must not be a dead end on macOS.
- `longhouse connect --install` is the canonical local-runtime repair/install verb after the CLI is present.
- The local runtime must ship as explicit versioned artifacts; the installer may not assume repo-local builds.
- On macOS, the ambient menu bar helper belongs to the same local-runtime lane as the engine service and app bundle, not a separate product path.
- All acquisition channels must converge on the same runtime state and install metadata.

## Install Source Policy

### Stable CLI MVP

- Canonical package source: PyPI
- Canonical package manager path: `uv tool install longhouse`
- Bootstrap convenience path: `curl -fsSL https://get.longhouse.ai/install.sh | bash`

The shell installer remains the thin bootstrap wrapper around the same package story users will use later for upgrades. Keep this path first-class for agents, Linux users, automation, and power users even after the macOS app path becomes the human default.

### Target macOS human lane

Canonical product shape:

- notarized `Longhouse.app`
- signed and notarized disk image around the app bundle

Behavior contract:

- opening the app directly must show setup, status, repair, or Longhouse in the browser
- the app may stay quiet in the menu bar after setup
- the app should delegate runtime repair to the same CLI/runtime seams instead of inventing a second installer stack
- public macOS artifact names must use `Longhouse`, not internal component names

### Canonical local runtime

After the CLI is present, one command owns local runtime repair and reinstallation:

- `longhouse connect --install`

That command is responsible for:

- ensuring the engine binary exists locally
- installing the engine launchd/systemd service
- installing provider hooks
- installing the ambient macOS `Longhouse.app` helper when enabled

This keeps the local runtime install surface singular even if the shell installer, onboarding flow, app bundle, and future Homebrew lane all call into it.

### Deferred

- Homebrew Cask
- preview channel
- signed release manifest with min-supported versions
- fully automatic runner and engine update coordination
- app-owned helper lifecycle through Apple-native service management

## Local Runtime Artifacts

The local runtime now has three published artifact classes:

- Python wheel: `longhouse`
- engine binary: `longhouse-engine-<platform>`
- transitional macOS app archive: `longhouse-local-health-app-darwin-arm64.zip`

Target public macOS artifact:

- `Longhouse-macos-<arch>.dmg`

Artifact policy:

- the `vX.Y.Z` GitHub release is the versioned source of truth for local runtime binaries
- the CLI version and runtime artifact version should match the same tag by default
- local/dev validation may override artifact sources explicitly
- on macOS, `connect --install` should install the ambient helper as `/Applications/Longhouse.app`
- raw menu bar/window executables are repo-local harness artifacts, not consumer release assets
- ZIP is transitional transport only; the website-facing macOS download should move to DMG

## macOS Trust Lane

`Longhouse.app` is now the canonical macOS ambient runtime artifact. For smoke and local packaging runs, ad-hoc signing is acceptable. For real stable semver releases, it is not.

Release policy:

- tags that match `vX.Y.Z` are `stable`
- any other release tag is `smoke`
- stable macOS releases must be Developer ID signed and notarized
- stable releases must fail in CI if the trust path is unavailable
- smoke releases may still use ad-hoc signing and skip notarization
- the CI lane should submit notarization before the long wait, so a slow Apple queue leaves recoverable artifacts plus `pending-apple` metadata instead of discarding the built app

Required GitHub secrets for stable macOS releases:

- `MACOS_SIGNING_CERT_P12_BASE64`
- `MACOS_SIGNING_CERT_PASSWORD`
- `MACOS_SIGNING_IDENTITY`
- `MACOS_NOTARY_APPLE_ID`
- `MACOS_NOTARY_TEAM_ID`
- `MACOS_NOTARY_APP_PASSWORD`

Expected packaging manifest for a healthy stable release:

```json
{
  "signing_mode": "developer-id",
  "notarization_status": "notarized",
  "artifacts": [
    {
      "bundle_id": "ai.longhouse.app",
      "app_name": "Longhouse"
    }
  ]
}
```

Fast operator checks before attempting to wire the GitHub secrets:

- `security find-identity -v -p codesigning`
- `xcrun notarytool history --keychain-profile <profile>`

If there is no local Developer ID identity or notary profile yet, the trust lane is blocked on Apple credential provisioning, not repo code.

Apple credential provisioning is currently an attended/manual operator flow.
Keep the one-off CSR, p12, and GitHub secret setup notes outside the repo, then
verify local identities and notary profiles with the commands above.

The shell installer should not know asset naming rules itself forever. The long-term goal is that the CLI/runtime layer owns artifact resolution while the shell script stays thin, and the macOS app path should reuse the same runtime layer instead of forking its own artifact logic.

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

### Canonical macOS packaging loop

Use the dedicated packaging smoke target when changing runtime artifact names, app-bundle metadata, or release scripts:

- `make test-runtime-packaging-macos`

This proves the canonical `Longhouse.app` archive can be built, ad-hoc signed, zipped, and structurally validated locally before touching a tag or GitHub Actions release run.

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
- app-first macOS validation on GitHub runners

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
- app-first macOS validation that proves `Longhouse.app` is a usable entry point

## Success Criteria

- New users have one obvious acquisition path for their audience:
  - `Longhouse.app` for macOS humans
  - shell or `uv` for CLI-first and agent installs
- Onboarding plus `connect --install` produce one coherent local runtime instead of separate manual engine/menu-bar steps.
- Clicking `Longhouse.app` is not a dead end.
- The ambient macOS menu bar is installed from the same runtime lane as the engine service and app bundle.
- The local installer smoke validates the real runtime path in a disposable `HOME`.
- GitHub Actions validates both the CLI-first and app-first happy paths.
- Future pivot to a bundled macOS app remains a control-adapter swap, not a rewrite of health classification or UI state contracts.

Do not implement yet:

- silent auto-update on startup
- Homebrew Cask distribution
- npm distribution
- multi-channel update policy
- release manifest service

## Definition Of Done

- users can install Longhouse through the bootstrap installer without touching repo internals
- users can ask the CLI whether they are outdated
- users can upgrade the CLI through a first-party `longhouse upgrade` command
- the CLI tells users when `longhouse connect --install` is still required
- install and upgrade can be exercised in a disposable sandbox without mutating David's real laptop environment
