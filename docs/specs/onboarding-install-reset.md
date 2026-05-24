# Onboarding And Install Reset

Status: Active
Owner: launch product
Updated: 2026-04-14

## Goal

Define one honest install and onboarding contract for launch.

The concrete single-state + single-reconciler design for local machine config
lives in `docs/specs/machine-state-reconcile.md`.

Users should not have to reverse-engineer the difference between:

- `Longhouse.app`
- `curl -fsSL https://get.longhouse.ai/install.sh | bash`
- `uv tool install longhouse`
- `longhouse onboard`
- `longhouse connect --install`
- the Machine Agent
- the Runtime Host

Those seams can remain in code. They must stop acting like separate products.

## Core Decision

Longhouse has one macOS product and two supported macOS acquisition methods.

- The product on macOS is `Longhouse.app`.
- The two supported macOS acquisition methods are:
  - direct app download
  - terminal bootstrap via `install.sh`
- Those two methods MUST converge on the same installed state.
- On macOS, `install.sh` is not a separate product story. It is an automation-
  and agent-friendly bootstrapper for the same app-first product.
- On Linux and other non-GUI/power-user environments, the CLI remains the
  primary product surface.
- `longhouse connect --install` remains the canonical repair verb.
- Trial-mode localhost runtime state MUST NOT silently overwrite durable
  machine-agent target state.

## Product Model

Longhouse on a Mac consists of four distinct components:

- **Desktop App**: `Longhouse.app`, the native macOS setup/status/repair UI.
- **Machine Agent**: the Rust engine that watches local hooks/outbox and ships
  session data from the machine where work happens.
- **Runtime Host**: the backend plus bundled web UI that serves the browser
  dashboard and durable state.
- **CLI / Installer Layer**: `longhouse`, `install.sh`, and repair verbs that
  bootstrap, script, and automate the other components.

These are parts of one product. They are not four separate products.

## Supported macOS Installed State

The only supported human-facing app path on macOS is:

- `/Applications/Longhouse.app`

A healthy macOS install is describable as:

- `Longhouse.app` present at `/Applications/Longhouse.app`
- one configured machine identity
- one durable machine-agent target
- one machine-agent service state
- one hook installation state
- one browser/default dashboard target
- one topology choice for this machine

Rules:

- `~/Applications/Longhouse.app` is unsupported.
- arbitrary unpacked `.app` paths are unsupported.
- on macOS, every installer/bootstrap path that installs the app MUST target
  `/Applications/Longhouse.app`
- if `Longhouse.app` launches from the wrong path, it MUST block and say
  "move Longhouse.app to /Applications and relaunch."
- if both `/Applications/Longhouse.app` and an unsupported copy exist, the
  `/Applications` copy is authoritative and unsupported copies are blocked
- we do not carry broad legacy-path compatibility as part of the launch
  product contract.

## Supported macOS Acquisition Methods

### 1. Direct app download

This is the trust-forward human path.

Expected flow:

- download the signed app package
- drag `Longhouse.app` to `/Applications`
- open the app
- the app owns setup, status, repair, and browser handoff

This path must be truthful on its own. It cannot secretly depend on the user
already having completed a shell-first install.

### 2. Terminal bootstrap

This is the speed/automation/agent path.

Use cases:

- copy-paste install for devs
- onboarding by another agent
- non-interactive setup
- scripting and repeatability

On macOS, this path MUST install the same product state as the direct-download
path:

- install or update `Longhouse.app` into `/Applications`
- install the CLI so automation and repair remain available
- install the Machine Agent / hooks / related local runtime pieces through the
  shared installer seam
- open `Longhouse.app` when running interactively with a GUI available
- skip auto-open in non-GUI or explicitly non-interactive mode if needed

Rules:

- terminal bootstrap MUST NOT create a separate macOS product story
- terminal bootstrap MUST NOT install a different app path than the direct app
  path
- terminal bootstrap MUST NOT own a divergent long-term onboarding state
  machine

## CLI-First Path Outside the Mac App Story

The CLI remains first-class for:

- Linux
- non-GUI environments
- automation
- agents
- power users who prefer explicit commands

On non-macOS platforms, there is no requirement to install a desktop app.

On macOS, the CLI may still be used directly, but if it installs or repairs the
desktop app, it must target the same canonical app path and same installed
state as the direct-download path.

## Topology Intents

Install/onboarding must choose one explicit topology intent before mutating
shared machine state.

### Try on this Mac

- Runtime Host runs locally
- Machine Agent runs locally
- browser opens localhost
- this is explicitly trial-mode and non-durable

### Connect this Mac to an existing Longhouse host

- Machine Agent runs locally
- Runtime Host already exists elsewhere
- browser opens the existing host
- this flow MUST NOT silently start or reconfigure a local Runtime Host

### Make this machine the durable Longhouse host

- Runtime Host runs here intentionally
- Machine Agent may also run here if this is a working machine
- browser target and machine-agent target may coincide, but only by explicit
  setup intent

Rule:

- "localhost trial" is not the hidden fallback for every flow

## Entrypoint Responsibilities

Each entrypoint needs a narrow job.

### `Longhouse.app`

Job:

- native setup
- local status
- repair
- browser handoff

Must:

- work on first launch without prior shell bootstrap
- show setup if runtime pieces are absent
- show repair if install state is broken
- show a move-to-Applications blocker when launched from the wrong path
- call the shared install/repair seam underneath when mutation is required

Must not:

- act like a separate diagnostic sidecar
- assume the shell path already did the "real" install

### `scripts/install.sh`

Job:

- bootstrap the product for terminal users, agents, and automation

Must:

- install the CLI/tooling needed for automation and repair
- install or update `/Applications/Longhouse.app` on macOS
- hand off to the same shared install/repair seam used by the app
- open the app when interactive and appropriate

Must not:

- become a second permanent onboarding system
- leave macOS in a different installed state than the direct app path

### `longhouse onboard`

Job:

- choose topology
- orchestrate setup
- verify outcome
- hand off to the next surface

Must:

- delegate install mutation to the shared seam
- remain explicit about which topology it is setting up

Must not:

- remain a second full installer implementation forever
- survive launch as a second installer; if it is not reduced to a thin
  topology-selection/orchestration shim, it should be hidden or deprecated

### `longhouse connect --install`

Job:

- idempotent local install / repair

Must:

- restore the canonical local machine state
- remain safe to rerun repeatedly

### `longhouse serve`

Job:

- Runtime Host lifecycle only

Must not:

- silently redefine the durable machine-agent target

### `longhouse doctor`

Job:

- read-only diagnosis

Must not:

- mutate install state

## Ownership Boundary

There should be one shared local install/repair service for machine state
mutation.

That service owns:

- runtime artifact resolution
- desktop app installation/registration when applicable
- engine installation
- service registration
- hook installation
- machine identity persistence
- target URL persistence
- repair / reinstall logic

Everything else is an adapter:

- `Longhouse.app`: native UI adapter
- `install.sh`: bootstrap adapter
- `onboard`: topology/orchestration adapter
- `connect --install`: repair adapter

## Config Ownership

The following state domains must be separate:

- **Runtime Host config**
  - where a local Runtime Host listens
  - whether this machine is in trial mode or durable host mode

- **Machine Agent target config**
  - where this machine ships
  - durable machine identity
  - durable reconnect target

- **Browser default target**
  - what host/dashboard should open by default

- **Desktop app UI state**
  - local UI affordances only

Rule:

- starting a local Runtime Host for trial mode MUST NOT poison the durable
  machine-agent target config

## Launch Gates

The "one macOS product, two acquisition methods, one installed state" claim is
not true until all of the following are true:

1. The shared local install/repair seam is real.
   `Longhouse.app`, `install.sh`, `onboard`, and `connect --install` must all
   delegate install mutation to the same service instead of carrying divergent
   logic.

2. Runtime Host config and Machine Agent target config are split.
   Trial-mode localhost startup must not overwrite the durable shipping target.

3. `longhouse onboard` is reduced to a thin shim or removed from the primary
   launch story.
   We do not launch with two full installers.

4. CI proves convergence.
   A macOS app-first path and a macOS terminal-bootstrap path must both produce
   the same supported installed state.

## Launch-Week Public Contract

For launch, macOS public surfaces should present two install choices as two ways
to install the same product:

- **Download Longhouse.app**
  - trust-forward, signed native Mac path
- **Terminal Install**
  - best for agents, automation, and copy-paste power users

Rules:

- public copy MUST say these are two ways to install the same product
- public copy MUST NOT describe the app as "just a monitor"
- public copy MUST NOT imply that the terminal path installs a different Mac
  product

## What We Are Explicitly Not Doing

- no broad support matrix for arbitrary app bundle locations
- no second onboarding state machine hidden inside `install.sh`
- no public story where the shell path is "the real install" and the app is
  merely decorative
- no public story where the app path is "the real install" but the shell path
  quietly does something else

## Immediate Implementation Cuts

1. Collapse the macOS `install.sh` path into the same app-first installed state.
   On macOS it should install `/Applications/Longhouse.app`, install the CLI,
   and then hand off cleanly to the app/shared installer seam.

2. Split runtime-host config from durable machine-agent target config.
   This is the biggest correctness seam still left.

3. Reduce `onboard` to topology choice plus orchestration.
   It should stop behaving like a second installer.

4. Keep `connect --install` as the one repair verb.
   App repair and CLI repair should route through the same seam.

## Acceptance Criteria

This contract is only good enough if all of these are true:

- direct app download on macOS leads to setup/status/repair without prior shell
  bootstrap
- terminal bootstrap on macOS installs the same `/Applications/Longhouse.app`
  state and the same local runtime state
- non-GUI/agent terminal bootstrap can complete without requiring a GUI
- `connect --install` repairs back to the same state from missing/broken local
  runtime pieces
- `Longhouse.app`, CLI status, and browser handoff agree on machine identity
  and target URL
- starting a localhost Runtime Host for trial mode does not silently overwrite
  the durable machine-agent target

## Decision

The launch model is:

- one macOS product: `Longhouse.app`
- two macOS acquisition methods: app download and terminal bootstrap
- one macOS installed state: `/Applications/Longhouse.app` plus the shared
  local runtime wiring underneath
- one repair seam: `longhouse connect --install`
- one CLI-first story for Linux/non-GUI/automation

Everything else is implementation detail.
