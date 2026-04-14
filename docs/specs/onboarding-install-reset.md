# Onboarding And Install Reset

Status: Proposed
Owner: launch product
Updated: 2026-04-14

## Goal

Reset Longhouse onboarding and local install around one honest product story.

Users should not have to reverse-engineer the difference between:

- the shell installer
- `longhouse onboard`
- `longhouse connect --install`
- `Longhouse.app`
- launchd/systemd plumbing
- the local runtime host versus the machine agent

Those seams can remain in code. They must stop acting like separate products.

This spec formalizes the onboarding/install reset and should be treated as the
decision document for:

- local acquisition channels
- first-run setup behavior
- repair ownership
- install destination policy
- config/state ownership boundaries

It extends the direction in `docs/specs/local-app-product-unification.md` and
turns that direction into an explicit product contract.

## Scope

In scope:

- macOS human install and first-run behavior
- CLI-first local install behavior
- shared local runtime install/repair seams
- state ownership between app, CLI, runtime host, and machine agent
- migration from the current organically grown local install model

Out of scope:

- runner enrollment and runner-specific install UX
- hosted control-plane signup/billing flows
- Linux desktop packaging
- final Apple-native helper lifecycle replacement

## Decision Summary

The product decision is:

- `Longhouse.app` is the canonical macOS human entrypoint
- `longhouse` is the canonical CLI/power-user/automation entrypoint
- both acquisition lanes must converge on one shared local runtime installer
- `longhouse connect --install` remains the canonical repair verb
- the public macOS app lane targets `/Applications/Longhouse.app`
- trial-mode localhost runtime state must not silently overwrite durable
  machine-agent target state

## Normative Rules

- macOS human onboarding MUST be valid through direct app launch.
- CLI onboarding MUST be valid without the app.
- The app, CLI, and repair flow MUST converge on the same local runtime state.
- Only one shared installer service may mutate local runtime install state.
- `onboard` MUST orchestrate; it MUST NOT grow bespoke install logic.
- `serve` MUST manage runtime-host lifecycle only; it MUST NOT implicitly
  redefine durable machine-agent target config.
- The desktop app MUST NOT be marketed as a monitor/helper product.
- The CLI-first lane MUST NOT silently install a human-facing app into
  `~/Applications` as if that were equivalent to a normal Mac app install.
- Repair MUST be able to migrate old install locations and legacy launchd labels.

## First-Principles Rules

1. One visible owner per audience.
   On macOS, the visible owner is `Longhouse.app`.
   On Linux and for power users, the visible owner is the CLI.

2. One canonical installed state.
   Different acquisition channels may differ in transport, but they must converge
   on the same local runtime state.

3. One installer seam.
   There may be many entrypoints, but only one code path should mutate local
   runtime state.

4. One repair seam.
   Repair must be idempotent, explicit, and able to restore the canonical local
   runtime without the user understanding implementation details.

5. No hidden role switching.
   Trial-mode localhost runtime, durable self-hosted runtime, and hosted runtime
   are different topologies. The product must say which one the user is setting up.

6. Public install language must match installed reality.
   If we say "download Longhouse.app", the app path must be a truthful zero-to-one
   path, not a thin monitor that assumes shell setup already happened elsewhere.

## Current-State Audit

### What exists today

- `scripts/install.sh`
  Installs the CLI, mutates shell PATH, and usually runs onboarding.

- `longhouse onboard`
  Starts the local runtime host, installs the machine agent when possible,
  imports existing sessions, verifies ingest, optionally seeds demo data,
  and opens the browser.

- `longhouse connect --install`
  Installs the machine agent service, hooks, engine binary, and optional
  macOS desktop app.

- `Longhouse.app`
  Runs the menu bar/status UI, but today it still depends on the CLI/runtime
  layer for status collection and repair.

- `longhouse serve`
  Starts the local runtime host and writes local URL config used by other
  machine-local components.

### What the product claims

- macOS humans should think in terms of `Longhouse.app`
- `connect --install` is the canonical local repair seam
- all channels should converge on one coherent local runtime

### What the code actually does

- the macOS app install destination is currently `~/Applications/Longhouse.app`
- the desktop app path is written directly into a launchd plist
- the app is supervised as a launch agent, so path becomes part of install state
- the app is still a wrapper over `local-health --json`
- the app repair path still shells out to `longhouse connect --install`
- `onboard` still acts like a second installer and a second orchestrator
- `serve` still writes `~/.claude/longhouse-url`, which overlaps with machine-agent
  connection state

## Main Contradictions

### 1. We still have multiple installers

We say there is one repair seam, but in practice we still have at least four
mutating entrypoints:

- shell installer
- onboarding wizard
- `connect --install`
- direct macOS app install + launchd registration

That creates drift, duplicate decisions, and inconsistent failure modes.

### 2. The macOS app is visible, but not truly authoritative

`Longhouse.app` is presented as the human product surface, but it still behaves
like a thin health wrapper around the CLI/runtime stack.

That is the core reason the product feels organically grown:

- the app is visible
- the CLI is still operationally in charge
- launchd owns background lifecycle
- onboarding owns first-run orchestration

Too many owners.

### 3. We conflate runtime-host setup with machine-agent setup

The launch story is supposed to distinguish:

- Runtime Host: where durability lives
- Machine Agent: where sessions are observed and shipped

But the current local onboarding path still bundles all of this into one wizard
that starts localhost services, installs agent hooks, imports data, verifies
ingest, and optionally seeds demos.

That was fine for early iteration. It is now too muddy.

### 4. Trial-mode localhost state leaks into durable config seams

`serve` and local onboarding write localhost URLs into the same config area that
machine-local shipping and app repair later rely on.

That makes "I started a local server" and "this machine should ship to my durable
Longhouse URL" dangerously adjacent states.

The product model says these are different topologies. The current config seams
still blur them.

### 5. The macOS install destination is technically valid but product-wrong

Installing into `~/Applications` is legal and admin-friendly.

It is also the wrong default product choice for a human-facing Mac app because:

- users expect `/Applications`
- Finder's normal Applications affordance points there
- drag-install mental models assume that location
- launchd path wiring turns the choice into persistent hidden state

This creates avoidable confusion exactly where the product should feel native.

### 6. The app lifecycle is still implementation-first

The menu bar app still inherits early "monitor" DNA:

- status-first
- repair via CLI shell-out
- ambient helper semantics
- limited native lifecycle affordances

That is how a helper behaves, not how a primary Mac app behaves.

## Product Reset

### Audience split

There are only two user-facing local acquisition stories:

#### A. macOS human path

Entry product: `Longhouse.app`

Meaning:

- drag to `/Applications`
- open the app
- the app handles setup, status, repair, and browser handoff

The app may still call shared installer/runtime code underneath.
It may not require users to understand those seams.

#### B. CLI / Linux / agent path

Entry product: `longhouse`

Meaning:

- install with shell bootstrap or `uv tool install`
- run explicit setup or repair
- script and automate freely

## Topology Model

The product must treat these as distinct setup intents:

### Try on this Mac

- Runtime Host runs locally
- Machine Agent runs locally
- browser opens to localhost
- user is in explicitly non-durable trial mode

### Connect this Mac to an existing Longhouse host

- Machine Agent runs locally
- Runtime Host already exists elsewhere
- browser opens the existing host
- this flow must not start or reconfigure a local Runtime Host unless the user
  explicitly asks for that

### Make this machine a durable Longhouse host

- Runtime Host runs here intentionally
- Machine Agent may also run here if this is a working machine
- browser/dashboard target and machine-agent target may coincide, but only by
  explicit setup intent

Product rule:

- setup MUST ask or infer which topology is intended before mutating shared
  install/config state
- "localhost trial" is not a hidden fallback for every flow

## Entrypoint Contract

Each entrypoint needs a narrow, explicit job.

### `Longhouse.app`

Job:

- human-facing setup
- local status
- repair
- browser handoff

Must:

- work on first launch without prior shell bootstrap
- show setup if CLI/runtime are absent
- show repair if install state is broken
- show status and open-browser affordances when healthy

Must not:

- assume the CLI/runtime were installed some other way
- act like a separate diagnostic sidecar product

### `scripts/install.sh`

Job:

- bootstrap the CLI lane

Must:

- install the CLI/tooling
- record install metadata
- call the shared orchestration path when appropriate

Must not:

- become a second full installer stack
- be presented as the primary human macOS story

### `longhouse onboard`

Job:

- choose topology
- orchestrate first-run setup
- verify outcome
- hand off to the next surface

Must:

- delegate all local runtime mutation to the shared installer seam
- remain explicit about whether it is setting up localhost trial mode,
  remote-host connection, or durable self-host

Must not:

- directly own service install, hook install, desktop app install, and config
  mutation as a separate parallel implementation forever

### `longhouse connect --install`

Job:

- idempotent local runtime install/repair

Must:

- restore the canonical local runtime state
- migrate legacy install locations and labels where needed
- remain safe to rerun repeatedly

### `longhouse serve`

Job:

- start and manage Runtime Host lifecycle

Must:

- own runtime-host process concerns only

Must not:

- silently redefine machine-agent target config
- be the hidden owner of app/browser target state outside explicit localhost
  trial setup

### `longhouse doctor`

Job:

- read-only diagnosis

Must not:

- mutate install state

## Canonical Local Runtime State

The shared installer seam must converge all acquisition channels onto one
describable machine state.

A healthy local machine should be describable as:

- one configured machine identity
- one configured Longhouse URL target
- one machine-agent service state
- one hook installation state
- one runtime artifact state
- on macOS, one desktop app state

Additionally, the system should know:

- which topology this machine was set up for
- what browser/dashboard URL should open by default
- whether the Runtime Host is local, remote, or absent on this machine

No separate notions of "monitor installed", "menu bar helper installed", and
"real app installed".

## Config Ownership

The current reset requires a clean ownership split between state domains.

### Install metadata

Purpose:

- how Longhouse was acquired
- what version/channel is installed
- what migration rules apply

### Runtime Host config

Purpose:

- where a local Runtime Host listens
- whether this machine is running in localhost trial mode or durable host mode

### Machine Agent target config

Purpose:

- where this machine ships and reconnects
- machine identity and durable target URL

### Desktop app state

Purpose:

- local UI/runtime affordances only
- never the hidden source of truth for machine target selection

Rule:

- localhost Runtime Host state and durable machine-agent target state MUST be
  separate concepts, even if some flows initialize them to the same value

## Install Destination Policy

### Human macOS lane

Canonical destination:

- `/Applications/Longhouse.app`

Reason:

- matches user expectation
- matches drag-install mental model
- matches Finder/Spotlight conventions
- makes the product feel like a normal Mac app

### Transitional compatibility

We currently have installs under `~/Applications/Longhouse.app`.

Migration rules:

- repair MUST detect legacy `~/Applications` installs
- repair MUST rewrite launchd state to the canonical app path
- migration MAY move the app automatically or prompt, but it may not leave the
  machine in a split-brain state

### CLI-first lane

CLI-first setup may remain admin-light.

But if the CLI lane installs a desktop app at all, it must either:

- install the real app in its canonical location explicitly, or
- leave the app absent and tell the user so explicitly

It must not silently install the human-facing app into `~/Applications` and
pretend that is the normal Mac product path.

## Canonical Installer Seam

Keep one shared installer service under all mutation paths.

That service should own:

- install metadata
- engine/runtime artifact resolution
- service registration
- hook installation
- desktop app registration when applicable
- path migration when install location changes

Everything else becomes a thin adapter:

- shell installer: bootstrap + call installer seam
- `onboard`: orchestration only, no custom mutation logic
- `connect --install`: repair wrapper over installer seam
- `Longhouse.app`: native setup/repair wrapper over installer seam

## First-Run State Model

For the macOS app lane, first run should collapse to a small explicit state
machine:

- `setup-required`
- `installing`
- `choose-topology`
- `healthy`
- `repair-required`
- `host-unreachable`
- `auth-required`

Rules:

- every direct app launch must resolve to one of those states
- every state must have a visible next action
- no broken status panel should stand in for setup
- no CLI error text should be the primary first-run UX

## What Should Change

### 1. Split onboarding into topology choice first

Before mutating anything, the product should ask one explicit question:

- Are you trying Longhouse on this Mac?
- Connecting this Mac to an existing Longhouse host?
- Setting up a durable Longhouse host on this machine?

Today those paths are too blended.

### 2. Reduce `onboard` to orchestration, not installation logic

`onboard` should stop directly acting like a second installer.

It should:

- choose topology
- call the shared installer seam with explicit intent
- verify
- open the right next surface

It should not independently own server start, service install, hook setup,
demo seeding, and browser logic as one monolith forever.

### 3. Make `Longhouse.app` a truthful app-first path

Direct app launch on a clean Mac must be valid.

That means:

- if CLI/runtime are absent, show setup
- setup bootstraps the shared installer seam
- health/repair live in the app
- browser handoff happens after healthy setup

No fake "download app, then use shell for the real install" story.

### 4. Move the macOS canonical install destination to `/Applications`

This is the human path. Optimize for native expectation.

Rules:

- public app lane installs to `/Applications/Longhouse.app`
- repair must detect and migrate existing `~/Applications/Longhouse.app`
- launchd plist regeneration must follow the canonical app path

If permission prompts are unacceptable for some lanes, that is a lane-specific
transport problem, not a reason to keep the human default in the wrong place.

### 5. Separate runtime-host config from machine-agent target config

We need distinct state for:

- local server "where this runtime host is listening"
- machine-agent "where this machine should ship"
- browser/dashboard "what host to open"

They can default together in trial mode.
They should not be one implicitly shared file forever.

### 6. Remove legacy internal naming from product-critical seams

The code still preserves old `local-health`/helper language in too many places.
Compatibility aliases are fine. Product ownership logic should stop being built
on those names.

### 7. Make browser handoff explicit, not magical

The browser is the main working surface, but it is not the owner of setup.

Rules:

- healthy setup should open the correct dashboard target
- app and CLI should both know which target they intend to open
- browser handoff should reflect the chosen topology, not a stale localhost
  assumption

## Proposed Near-Term Cuts

### P0: Product truth cleanup

- freeze the human macOS story as `Longhouse.app`
- document `/Applications` as the target destination
- explicitly state that current `~/Applications` behavior is transitional
- stop describing the app as a monitor/helper in any user-facing copy

### P1: Installer seam hardening

- move all local runtime mutation behind one installer service
- make `onboard`, shell install, and app setup adapters only
- add install-location migration support
- split runtime-host URL state from machine-agent target URL state

### P2: App-first setup

- clean first-launch state machine inside the app
- setup / repair / healthy / host-missing / auth-needed become explicit app states
- remove app dependence on "hope CLI already exists" as the normal path

### P3: Lifecycle cleanup

- revisit launchd ownership
- keep launchd as implementation detail if needed
- move toward app-owned lifecycle only after the install story is coherent

## Acceptance Criteria

This reset is only successful if all of these work:

### Fresh macOS human install

- drag `Longhouse.app` to `/Applications`
- open app
- app can complete first-run setup without shell bootstrap
- healthy result opens the right browser target and leaves the app in a healthy state

### Fresh CLI install

- `uv tool install longhouse`
- run onboarding/setup explicitly
- machine reaches the same healthy local runtime state as the app lane

### Legacy migration

- machine with `~/Applications/Longhouse.app` upgrades successfully
- launchd state is rewritten coherently
- no duplicate app-owner confusion remains after repair

### Trial mode safety

- starting a localhost Runtime Host for trial mode does not silently poison a
  remote durable machine-agent target

### Repair

- `longhouse connect --install` restores healthy state from broken/missing
  hooks, service, engine, desktop app path, or stale labels

### Cross-surface agreement

- app, CLI, and browser agree on machine identity, health state, and target URL

## Open Design Constraints

- Public macOS expectations push us toward `/Applications`.
- Automation and admin-light CLI flows push us away from requiring privileged
  mutation for every install path.
- The right answer is not "keep the wrong app path forever"; it is to separate
  human app install expectations from CLI automation constraints cleanly.

## What To Keep

- `longhouse connect --install` as the canonical repair verb
- the CLI-first lane for Linux, automation, and power users
- the local runtime installer service as the shared mutation seam
- the browser as the main working surface after setup

## What To Kill

- any product story where the app is "just a monitor"
- any second installer logic in onboarding
- any assumption that localhost trial-mode config is the same thing as durable
  machine-agent target config
- the implicit idea that `~/Applications` is a good default human install target

## Decision

The clean launch model is:

- macOS humans install `Longhouse.app` into `/Applications` and open it
- the app owns setup, status, repair, and browser handoff
- CLI users install `longhouse` and use explicit commands
- both paths call the same installer service and converge on the same machine state

Everything else is implementation detail or migration debt.
