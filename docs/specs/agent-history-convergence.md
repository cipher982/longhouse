# Agent History Convergence

Status: Active launch simplification
Owner: Longhouse session kernel
Updated: 2026-04-15

## Goal

Stop running two adjacent agent-history systems.

Longhouse should own:

- raw session ingest
- session archive storage
- search, recall, tail, wall, and session detail
- machine-facing session APIs

Life Hub should not own a second raw agent transcript archive.
If Life Hub needs agent context, it should consume Longhouse as a client or
store a narrow derived record that references a Longhouse session id.

## The Problem

Today we have duplicated system boundaries:

- Longhouse stores agent sessions/events in its own runtime archive and serves
  them from `/api/agents/*`.
- Life Hub also stores agent sessions/events in `agents.sessions` /
  `agents.events`, has its own ingest pipeline, its own query endpoints, its
  own dashboard, and its own MCP SQL surface.
- Sauron runs a sync job whose only purpose is to copy Longhouse session history
  into Life Hub.

That duplication creates exactly the wrong failure mode:

- one poison-pill source line can break the copy pipeline
- repair needs to reason about two schemas and two APIs
- feature work can fork into "Longhouse way" vs "Life Hub way"
- agent history starts behaving like an integration problem instead of a core
  product capability

## First Principles

1. One raw ledger per capability.
2. Projections are disposable.
3. Derived views must never be mistaken for truth.
4. Cross-system joins should pass identifiers, not duplicate full raw history.
5. If a surface is not launch-critical, delete or freeze it instead of
   preserving parallel ownership.

## Canonical Ownership

### Longhouse owns

- raw provider/session/source-line ingest
- transcript/event persistence
- search, recall, timeline, session detail, wall, tail
- machine-facing agent APIs and MCP continuity tools
- session capability and control metadata

### Life Hub keeps

- tasks
- smart home
- health / infra / email / medicine / docs
- optional narrow references to Longhouse sessions for downstream workflows
  such as work items, hindsight, or notes

### Life Hub does not keep

- a second raw transcript archive
- a second agent search product
- a second agent dashboard product
- a second shipper / watchdog / replay path for Longhouse session history

## Data Contract Between Systems

If Life Hub needs to attach agent context to another domain object, store only
durable references plus minimal derived summaries.

Examples:

- `longhouse_session_id`
- `longhouse_url`
- `project`
- `provider`
- `started_at`
- a narrow derived summary or note written by an explicit workflow

Do not copy all raw events into Life Hub just to enable joins.

## Migration Plan

### Phase 1: Stop the bleeding

- Retire the Life Hub agent dashboard as a product surface.
- Redirect `/agents/*` in Life Hub to Longhouse.
- Remove the Agents nav entry from Life Hub.
- Disable the Sauron `life-hub-agent-sync` and `agent-ingest-watchdog` jobs.

This cuts off new duplication immediately.

### Phase 2: Freeze duplicate machine/API surfaces

- Mark Life Hub `/ingest/agents/*`, `/query/agents/*`, and agent-history MCP
  surfaces as deprecated.
- Prefer hard failure or explicit redirect over silently serving stale data.
- Point operator workflows to Longhouse `/api/agents/*` or Longhouse MCP.

### Phase 3: Keep only narrow derived integrations

- For hindsight or task workflows, pass `longhouse_session_id` and fetch what is
  needed from Longhouse on demand.
- If a workflow genuinely needs a cached derivative in Life Hub, cache a small
  derived record with provenance and a refresh path.

### Phase 4: Delete dead code

- Remove Life Hub agent shipper docs and code paths.
- Remove duplicate agent dashboard endpoints and templates from Life Hub.
- Remove old sync/repair jobs from Sauron.
- Drop or archive Life Hub `agents.*` tables only after no remaining live
  workflow depends on them.

## Explicit Non-Goals

- Do not move Life Hub tasks, smart home, or personal data into Longhouse.
- Do not introduce a new control plane between Life Hub and Longhouse.
- Do not keep both raw stores and merely "sync better."

## Immediate Rule

If a new feature needs raw session history, build it on Longhouse.
If another system needs agent context, reference Longhouse instead of copying it.
