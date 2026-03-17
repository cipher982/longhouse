# Continuity Memory Boundary

Status: In progress

## Executive Summary

Longhouse should keep a thin continuity-memory layer, not grow a second product for knowledge management, planning, or ops monitoring.

The raw session archive is already the source of truth. Search and recall are the retrieval layer. The only additional memory Longhouse needs is a small curated set of reusable learnings that help future sessions avoid repeated mistakes.

Everything else should move out of that path:

- operational alerts belong to reliability / health
- reflection output belongs to an internal draft queue, not canonical memory
- proposals are not part of the primary launch surface

This pass finishes that boundary split.

## Why This Belongs In Longhouse

The thin version belongs in Longhouse because it directly improves continuity:

- `query_insights` gives agents a compact “known gotchas” layer above raw recall
- briefings can inject a small set of trusted project learnings at session start
- manual insights let useful knowledge survive across providers, machines, and sessions

The thicker version does **not** belong in Longhouse core:

- automatic reflection loops
- review queues as product surface
- ops alerts mixed into memory
- a standalone “distill my engineering work into knowledge/tasks” workflow

That would be a different wedge than Longhouse’s core product.

## Product Boundary

### Longhouse Core

- session archive (`sessions`, `events`)
- search / semantic search / recall
- briefings assembled from recent sessions plus curated gotchas
- curated insights as continuity memory

### Reliability / Ops

- stale-agent alerts
- ingest-stale / ingest-recovered alerts
- runner health incidents
- job health failures and other operational attention flows

### Internal Admin Tooling

- manual reflection runs
- reflection output review
- proposal / draft queue, if retained

## Current State

- `Insight` is now cleaner than before: new writes carry `origin`, and explicit `system` rows are hidden from normal reads.
- Operational alerts are still **stored in `insights`**, just filtered out of default reads.
- Briefings still include approved action proposals.
- Reflection is paused by default, but the code path still creates canonical insights and proposals when manually triggered.
- There is still no small browser curation surface for the insight corpus.
- `ProposalsPage` still exists as a route even though it is not part of the primary navigation.

## Target State

### 1. Archive stays the source of truth

- Session logs remain canonical.
- Search / recall remain the first stop for detailed prior-art retrieval.

### 2. Insights become curated continuity memory only

`Insight` should hold:

- manual learnings logged by agents or users
- optionally reviewed/refined reflection-derived learnings
- reusable gotchas, deployment traps, and durable patterns

`Insight` should **not** hold:

- operational alerts
- transient monitoring signals
- draft tasks or planning artifacts

### 3. Operational alerts move to a dedicated incident domain

Add a small durable incident model for tenant-local reliability events, modeled more like `RunnerHealthIncident` than `Insight`.

Candidate shape:

- `incident_type`
- `source`
- `status` (`open` / `resolved`)
- `summary`
- `context`
- `opened_at`
- `last_observed_at`
- `resolved_at`
- `dedupe_key`

Bounded first use:

- stale-agent job writes incidents
- ingest-health job writes incidents
- reliability/admin APIs can list recent incidents

### 4. Briefings stay focused on context, not planning

Briefings should contain:

- recent session summaries
- curated insights / known gotchas

Briefings should not contain:

- approved proposals
- draft actions
- operational alert rows

### 5. Reflection becomes optional admin analysis, not memory author

Near-term launch posture:

- reflection remains manual/admin only
- it is not part of the main product story
- it is not linked from the primary workflow

If kept long-term:

- reflection output should become a draft/review queue
- reflection should not directly write canonical `Insight` rows without approval

### 6. Insight curation gets a tiny browser surface

If insights are canonical continuity memory, they need minimal human curation without SQLite surgery.

Minimal surface:

- list insights
- filter by project / type / origin / archived state
- archive / unarchive

Bounded requirement:

- not a major new product area
- not part of primary nav
- reachable from continuity-adjacent UI such as Briefings or an admin/settings path

## Decisions

### Decision: Keep insights as Longhouse core

**Choice:** Keep `log_insight`, `query_insights`, and briefing gotchas as part of Longhouse.

**Why:** This is the thin continuity layer above raw search/recall.

### Decision: Move operational alerts out of `insights`

**Choice:** Stop using `Insight` as an alert sink and add a dedicated incident path.

**Why:** Filtering hidden system rows is a good cleanup, but it is still the wrong domain model.

### Decision: Remove proposals from the primary product surface

**Choice:** Hide or remove `ProposalsPage` from the normal app surface and stop feeding proposals into briefings.

**Why:** Proposals are planning artifacts, not continuity memory, and they are not part of the current product wedge.

### Decision: Reflection is admin-only unless proven valuable

**Choice:** Keep reflection manual and low-visibility. Do not invest in it as a user-facing product surface before launch.

**Why:** It is optional analysis, not core continuity.

### Decision: Add a minimal insight-curation surface instead of a big insights product

**Choice:** Add archive/unarchive and inspection only.

**Why:** This gives the corpus curation without turning Longhouse into a separate knowledge-management app.

## Scope

In scope:

- define the target domain split: archive vs insights vs incidents vs reflection drafts
- add a tenant-local incident model/API for ops alerts
- remove proposal data from briefing composition
- hide/remove proposals from the primary browser product surface
- add minimal insight archive/unarchive curation
- align docs and product copy with the trimmed continuity story

Out of scope:

- building a full team knowledge base product
- making reflection a core launch feature
- designing a complex task management system around proposals
- merging all reliability domains into one universal incident framework in this pass
- deleting every dormant reflection code path immediately

## Success Criteria

1. Stale-agent and ingest-health jobs no longer create `Insight` rows.
2. Tenant ops alerts are visible through a reliability/admin incident surface instead of insight queries.
3. `query_insights` and briefing “Known gotchas” return curated continuity memory only.
4. Briefings no longer include approved proposals or other planning artifacts.
5. `ProposalsPage` is no longer part of the normal product surface, and docs stop presenting proposals as a meaningful user-facing feature.
6. Browser users have a minimal way to inspect and archive/unarchive insights without DB access.
7. Reflection remains paused by default and is documented as optional admin tooling, not a core product feature.

## Implementation Order

1. Move ops alerts into incidents.
2. Remove proposals from briefing assembly and primary UI surface.
3. Add minimal insight archive/unarchive curation.
4. Tighten docs and product descriptions around the final boundary.
5. Re-evaluate whether any reflection/proposal code should survive launch after that split lands.
