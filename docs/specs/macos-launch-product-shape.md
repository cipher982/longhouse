# macOS Launch Product Shape

Status: Active
Owner: launch product
Updated: 2026-04-10

## Goal

Define the clean macOS product shape for launch without breaking the existing machine contract or the CLI-first power-user path.

## Decision

Longhouse launches as one product with multiple acquisition channels.

On macOS, the human-facing product should be `Longhouse.app`.

That does **not** mean the CLI goes away. It means the macOS human story stops teaching shell bootstrap and background helpers as separate concepts. The app becomes the visible owner, while the CLI, runtime artifacts, and launchd plumbing stay behind a stable contract.

## Product Rules

- `Longhouse.app` must never be a dead end.
- Clicking `Longhouse.app` should always lead to one of three outcomes:
  - open the status or setup window
  - open Longhouse in the browser
  - show the repair path clearly
- The app should be quiet by default:
  - no Dock icon required
  - persistent menu bar presence is valid
  - the main browser dashboard remains the primary workspace
- The browser is the main work surface, not the installer or health owner.
- `longhouse doctor` and `longhouse connect --install` remain the repair verbs.
- All install channels must converge on the same local runtime state.

## Channel Model

### Human macOS path

Primary launch target:

- download `Longhouse.app` in a notarized direct-download package

Expected result:

- the app can be opened directly from `Applications`
- first launch or reopen gives a visible status / setup / repair surface
- the background runtime can stay on quietly
- the browser dashboard opens when the machine is healthy

### Agent and power-user path

Keep these first-class:

- shell bootstrap
- `uv tool install longhouse`
- PyPI package `longhouse`

Why:

- agents can script them
- headless and Linux installs still need them
- they remain the cleanest low-friction path for machine setup and automation

### Secondary macOS distribution path

Add later:

- Homebrew Cask

Use it as transport and upgrade convenience, not as a separate lifecycle model.

## Near-Term Architecture

Short-term launch shape:

- the local server and engine service remain the real runtime
- the app bundle is the ambient macOS owner users can see
- launchd may still supervise the current helper path underneath
- the app opens a native status window when launched directly
- the app can live quietly in the menu bar when running in the background

Important rule:

- launchd remains an implementation detail, not part of the public product story

## Later Architecture

Once the launch path is stable, move helper lifecycle into Apple-native app ownership:

- app-managed login/background registration
- bundled helpers where needed
- the same health contract and repair surface preserved

That future move should be a control-adapter swap, not a product rewrite.

## Launch Success Criteria

The macOS launch path is good enough when:

- a user can install or receive `Longhouse.app` and understand what it does without reading shell docs
- clicking the app always produces an explicit outcome instead of a silent or broken state
- the browser, menu bar, and CLI all agree about install and health state
- agent installs still work cleanly through shell or PyPI paths
- CI proves both the app-first path and the CLI-first path on GitHub runners
