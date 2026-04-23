# User-Facing Health And Observability

Status: Draft
Owner: launch product
Updated: 2026-04-23

## Goal

Decide how Longhouse should present machine/session health and latency telemetry
to humans without turning the product into a generic ops dashboard.

This doc answers:

- what a self-hosted or hosted user should see inside Longhouse
- what belongs in the macOS menu bar
- what belongs in our internal hosted-ops surface
- where raw telemetry APIs fit for future agents

## Product Decision

Longhouse should ship **three** observability surfaces with different jobs:

1. **Runtime Host Health page** on every provisioned instance
   - primary human surface
   - tenant-scoped
   - built into Longhouse
2. **macOS menu bar health summary**
   - ambient, machine-local surface
   - only answers "what is happening on this Mac right now?"
3. **Hosted fleet health page**
   - internal/admin-only control-plane surface
   - only for post-launch multi-tenant regression detection across hosted users

We should **not** make Grafana the product surface.

Grafana remains optional infra for raw metric storage or internal debugging, but
it is not the thing Longhouse users should open to understand whether their
machines or sessions are healthy.

## Why

This follows the product thesis in `VISION.md`:

- Longhouse is mission control for real sessions on user-owned machines.
- Human surfaces are bundled views, not the boundary.
- The browser is the main workspace.
- The menu bar is a quiet ambient local surface, not the primary dashboard.

If observability exists only as an internal admin tool, self-hosted users lose
the ability to debug themselves. If it exists only as a generic chart wall, it
stops serving the actual launch story.

## Naming

For users, prefer **Health** over **Observability**.

Use:

- `Health`
- `Session health`
- `Machine health`
- `Slow turns`

Avoid as the primary product label:

- `Observability`
- `telemetry`
- `OTLP`
- `spans`

Implementation detail:

- current `/observability` can remain as the first dogfood route
- launch-facing browser copy should move toward `Health`

## Surface 1: Runtime Host Health Page

This is the primary user-facing surface and should live inside the main
Longhouse web app on every runtime host.

### Why this is the main surface

- it works for self-hosted and hosted users
- it matches the "browser is the main workspace" rule
- it can explain both per-machine and cross-machine problems
- it can deep-link directly into timeline and session detail

### Core user questions

The page should answer these in under 10 seconds:

1. Are my Longhouse machines healthy right now?
2. Are managed sessions slow right now?
3. Is the problem on one machine, one provider, or everything?
4. Is Longhouse slow, or is the provider/session itself slow?
5. What should I click next?

### Page shape

The launch-minimum page should have four blocks:

1. **Overview**
   - managed-turn `p50/p95`
   - slow-turn count
   - healthy/degraded/broken/offline machine counts
   - current time window
2. **Machines**
   - one row per machine
   - derived health state
   - heartbeat freshness
   - ship success/failure posture
   - backlog/dead-letter hints
   - build identity when relevant
3. **Slow turns**
   - recent outliers with provider, project, machine, total time
   - open session action
   - filter by provider, machine, project, threshold
4. **Diagnosis**
   - one or two high-confidence answers for the current window
   - examples:
     - `Claude turns are slower than baseline right now`
     - `cube is unhealthy and backing up ships`
     - `this looks fleet-wide, not machine-local`
     - `build 108bddbd regressed managed-turn latency on one provider`
   - every diagnosis block links to the affected machines, sessions, or filtered slow-turn view

### Required drill-downs

The Health page is not enough by itself. It must connect to the real work.

- machine row -> filtered machine detail or machine-scoped session list
- slow turn row -> session detail with turn timing breakdown
- diagnosis block -> filtered slow turns, provider slice, or machine slice

### Launch rule

Do not turn this into a general "metrics explorer".

It is a diagnosis page, not a chart workbench.

### Drill-down: Session Detail Health

Users do not only need a cross-session dashboard. They also need the answer for
one concrete session they are staring at.

Every managed session detail should expose:

- latest turn timing breakdown
- recent slow turns in that session
- current machine health badge
- provider health context
- clear callout when the machine is degraded/offline vs when the provider is slow

This should feel like part of the normal session detail, not a separate admin
mode.

This drill-down should be built from the canonical timing surfaces defined in
`docs/specs/session-observability-endgame.md`, not from a separate browser-only
truth.

## Surface 2: macOS Menu Bar

The menu bar is the ambient local surface for one machine, not the fleet view.

### What it should answer

On click, the menu bar should answer:

- is Longhouse healthy on this Mac?
- are any managed sessions currently attached, blocked, degraded, or orphaned?
- is shipping backing up?
- do I need to repair anything?

### What it should not try to do

The menu bar should not become:

- a multi-machine dashboard
- a latency percentile explorer
- a hosted-fleet view
- a replacement for the browser Health page

### Menu bar launch shape

This spec does not redefine the menu bar information architecture.

For the concrete healthy/broken panel shape, use
`docs/specs/macos-menubar-control-surface.md` as the source of truth.

This doc only adds the product boundary:

- the menu bar is local, ambient, and machine-scoped
- the menu bar should surface local managed-session degradation clearly
- the menu bar should hand off to the browser Health page for cross-machine and
  cross-session diagnosis

If a user wants cross-machine health or provider drift, send them to the
browser Health page.

## Surface 3: Hosted Fleet Health

This is a **post-launch** internal surface, not a launch-tier user surface.

We still need a fleet/operator view, but it is a different product surface.

This page should live in the control plane or another internal hosted-ops UI
and should answer:

- did a build regress managed-turn latency across tenants?
- is one provider drifting globally?
- are specific tenants or machines unhealthy?
- are we causing churn before users report it?

### Rules

- admin-only
- aggregate-first
- do not expose raw session bodies
- tenant names are okay for internal ops, but raw transcript content is not

### Minimum cards

- managed-turn `p50/p95` by provider
- slow-turn rate by build
- unhealthy machine counts by tenant
- top regressed tenants/builds/providers

This is where a Grafana-like experience is acceptable if needed, but it is
still better to start with a small first-party page over our own API contracts.

## API Contract Rule

The canonical machine/session truth must stay on `/api/agents/*`.

For the canonical operator read paths and timing surfaces, see
`docs/specs/session-observability-endgame.md`.

That contract should remain sufficient for:

- future agents
- CLI tools
- internal admin tools
- browser views

Browser convenience routes are allowed, but they are adapters over that same
truth.

Future control-plane fleet endpoints should be separate and explicit, not mixed
into the tenant runtime contract.

## Product Rollout Order

This rollout order is the **human-surface mapping** of the telemetry rollout in
`docs/specs/session-observability-endgame.md`. It does not replace that spec's
telemetry phases.

### Step 1: Turn dogfood observability into product Health

- keep the current runtime-host data model
- keep the current `/api/observability/*` dogfood routes if useful
- change browser copy and navigation toward `Health`
- add stronger deep links from slow turns to session detail
- add session-detail turn timing presentation

### Step 2: Add machine + session health affordances throughout the app

- machine health chips in timeline/session lists where relevant
- session-detail machine health block
- clearer "provider slow" vs "machine unhealthy" callouts

### Step 3: Upgrade the menu bar to machine-local mission control

- expose local slow/degraded managed-session states clearly
- keep cross-machine analysis in the browser
- add direct open-to-health-page handoff

### Step 4: Add hosted fleet health after launch

- internal control-plane page
- aggregate by tenant, provider, build, machine health
- regression-first workflow, not a raw metrics browser

## Success Criteria

This product shape is correct when:

- a self-hosted user can diagnose their own slowdown without our help
- a hosted user can tell whether their problem is machine, provider, or Longhouse
- the menu bar gives immediate local truth without pretending to be the whole product
- the browser remains the primary workspace for real diagnosis
- future agents can use the same canonical machine/session APIs without screen-scraping
- internal hosted ops can spot regressions across users without needing Grafana as the only usable view

## Non-Goals

- shipping a generic metrics query builder
- making the menu bar the main observability surface
- exposing OTEL terminology to normal users
- collapsing tenant runtime health and hosted fleet health into one mixed surface
