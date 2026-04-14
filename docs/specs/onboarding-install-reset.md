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

### Canonical local runtime state

A healthy local machine should be describable as:

- one configured machine identity
- one configured Longhouse URL target
- one machine-agent service state
- one hook installation state
- one runtime artifact state
- on macOS, one desktop app state

No separate notions of "monitor installed", "menu bar helper installed", and
"real app installed".

### Canonical installer seam

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
