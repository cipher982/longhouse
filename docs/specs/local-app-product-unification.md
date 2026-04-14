# Local App Product Unification

Status: Proposed
Owner: onboarding + desktop product
Updated: 2026-04-13

The formal onboarding/install contract now lives in
`docs/specs/onboarding-install-reset.md`.

## Goal

Make Longhouse on macOS read as one installable app and one coherent local product.

Users should not have to understand that the menu bar surface, the local runtime,
the Rust machine agent, and the CLI are separate implementation pieces. Those
seams can remain real in code, but they must stop behaving like separate products
in onboarding, packaging, naming, and repair flows.

## Problem

The current state fails in three ways at once:

- the landing page exposes an internal runtime artifact name and a ZIP transport
- the macOS direct download is only the ambient desktop surface, while the shell
  installer is the only path that actually sets up the full local runtime
- the source tree and release lane still frame the desktop app as "local health",
  which keeps pushing internal terminology back into product copy and asset names

This creates a mismatch between the intended story and the installed reality:

- intended story: "Download Longhouse.app"
- actual story: "Download a ZIP named after an internal health component, then
  hope the rest of the runtime already exists or can be repaired later"

## Product Decision

On macOS, Longhouse is one desktop app.

That app may live quietly in the menu bar once setup is complete, but the menu
bar presence, the status window, the setup flow, the repair path, and the
browser handoff are all the same product: `Longhouse.app`.

The desktop app is not a separate "health checker". It is the visible owner of
Longhouse on the local Mac.

## Public Naming Rules

Use these names in all user-facing surfaces:

- `Longhouse`
- `Longhouse.app`
- `Longhouse desktop app`

Do not use these names in user-facing surfaces:

- `local health`
- `local health app`
- `ambient local-health`
- `shipper`
- `daemon`

Implementation detail:

- "local health" may remain as an internal diagnostic concept only while the
  source tree is being renamed
- it must not appear in release asset names, landing-page copy, install
  metadata intended for humans, or app/window titles

## Internal Product Model

These seams stay real in code and docs, but they are not parallel user-facing
products:

- **Desktop App**: `Longhouse.app`, the visible local app on macOS
- **Runtime Host**: the local Longhouse backend + bundled web UI
- **Machine Agent**: the Rust engine that ships local events
- **CLI**: the scripting, automation, and repair surface

User-facing rule:

- on macOS, the app owns the story
- on Linux and for power users, the CLI remains first-class
- both channels must converge on the same local runtime state

## macOS Distribution Decision

For public macOS distribution, ship a signed and notarized disk image.

Chosen shape:

- public download artifact: `Longhouse-macos-<arch>.dmg`
- mounted payload: `Longhouse.app`
- install gesture: drag `Longhouse.app` to `/Applications`

Transitional rule:

- ZIP archives may continue to exist as CI or notarization intermediates
- ZIP must stop being the public website download target

Public artifact naming rule:

- all public macOS artifact names start with `Longhouse`
- no public artifact may contain `local-health`

## Install Contract

After successful macOS installation, regardless of channel, the machine should
end up in one coherent state:

- `/Applications/Longhouse.app` exists
- the `longhouse` CLI exists on PATH
- the local runtime can be started or opened
- the engine binary exists locally
- the machine-agent service is installed when supported and enabled
- the desktop app can show setup, status, repair, or open the browser dashboard
- local install metadata records enough state to explain upgrade and repair

Important product rule:

- the shell installer and the app-first path may differ in transport
- they may not differ in final installed state

## Shared Installer Seam

We should stop treating shell install, onboarding, `connect --install`, and the
desktop app as four installers.

Near-term decision:

- keep `longhouse connect --install` as the canonical human repair verb
- extract a shared Python installer service underneath it
- make onboarding, shell bootstrap, and the desktop app all delegate to that
  same installer service

Near-term bootstrap rule:

- if `Longhouse.app` launches on a Mac without the CLI/runtime present, it
  should bootstrap the CLI package using the same source policy as the shell
  installer, then invoke the shared local-runtime installer seam
- users should experience that as app setup, not as "now open Terminal and do
  the real install yourself"

## Source-Code Naming Cleanup

We need to stop encoding the wrong product model into the codebase.

Rename direction:

- `LOCAL_HEALTH_APP` -> `DESKTOP_APP`
- `local_health_ui` -> `desktop_app`
- "ambient local-health menu bar" -> "desktop app" or "Longhouse.app"
- public release asset names -> `Longhouse-*`

Compatibility rule:

- keep temporary aliases where needed for a short migration window
- do not preserve the old names in new product-facing code

## Release Lane Changes

The current landing page hardcodes a GitHub release asset URL and therefore
hardcodes internal packaging names into public UX.

Target behavior:

- landing page uses a stable app download endpoint, not a hardcoded GitHub
  asset filename
- release automation is free to rename or swap the underlying artifact
  (`.dmg`, universal build later, preview channel later) without frontend code
  changes

Near-term release tasks:

- build `Longhouse.app`
- package it into a signed/notarized DMG
- publish the DMG as the public Mac asset
- keep any ZIP only as an internal or transitional artifact

## Staged Implementation

### Stage 1: Public truth cleanup

Goal:

- stop leaking internal naming into onboarding immediately

Changes:

- add a stable macOS app download endpoint instead of hardcoding GitHub release
  asset names in the landing page
- rename public release assets away from `local-health`
- update landing, README, and release metadata to describe one desktop app, not
  a status helper

### Stage 2: Shared installer extraction

Goal:

- make all install paths converge through one runtime installer

Changes:

- extract the current `connect --install` behavior into a reusable installer
  service
- have `longhouse onboard` call that installer service directly instead of
  manually reconstructing install steps
- keep `longhouse connect --install` as a thin CLI wrapper over the same service

### Stage 3: App-first setup

Goal:

- make direct app launch a truthful zero-to-one path

Changes:

- launching `Longhouse.app` without an installed runtime shows setup, not a
  broken status panel
- setup installs the CLI/runtime through the shared installer seam
- healthy setup ends by opening Longhouse in the browser and leaving the app
  quietly available in the menu bar

### Stage 4: Unified status and repair

Goal:

- make the app feel like the owner of the local Mac experience

Changes:

- the app opens a native status window on direct launch or reopen
- repair, logs, update, and open-dashboard actions stay inside the app surface
- diagnostics still use the existing local-health schema under the hood, but
  that diagnostic naming no longer leaks into the visible product

### Stage 5: Internal rename completion

Goal:

- remove the confusing mental model from the source tree

Changes:

- rename core desktop runtime components from `local_health_*` to `desktop_app_*`
- rename the published runtime component and manifest fields
- keep temporary read compatibility only where existing installs require it

## Non-Goals

- bundling the runner into the first app-install cleanup
- changing Linux onboarding to mirror the macOS app lane
- rewriting the engine service architecture before fixing naming and install
  truthfulness
- moving immediately to app-owned `SMAppService` lifecycle management before the
  app-first setup path is honest

## Success Criteria

This pivot is successful when:

- a new Mac user can download Longhouse and understand what they installed
  without opening shell docs
- the website offers one honest macOS download with a human artifact name
- opening `Longhouse.app` can complete setup instead of only diagnosing a
  missing runtime
- shell install and app-first install produce the same local runtime state
- the codebase no longer trains contributors to think of the desktop app as
  "just the local health checker"
