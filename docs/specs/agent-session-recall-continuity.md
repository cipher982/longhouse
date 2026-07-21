# Agent Session Recall And Continuity

Status: Proposed epic
Date: 2026-07-19
Owner: Longhouse

## Summary

Longhouse must let a new agent find prior work, understand what happened, and
continue from the right state without copying an entire raw transcript into its
context.

The archive already preserves the necessary evidence. The missing product is a
reliable progressive-disclosure path over that evidence:

```text
discover a session
  -> read a clean, source-linked conversation projection
  -> inspect selected raw evidence when needed
  -> continue with the receiving model doing the synthesis
```

This epic repairs the currently broken recall/search contract, aligns every
client with the canonical cursor-based event store, and adds a provider-neutral
evidence projection for agent handoff. It deliberately does not make a
precomputed AI summary the source of truth. Raw session evidence remains
canonical; indexes, titles, summaries, and evidence packs are disposable views.

## Pre-Launch Convergence Decision

Longhouse has zero external users. This epic chooses one architecture and
deletes the alternatives instead of preserving compatibility:

- catalogd owns canonical session metadata and event/source locators;
- searchd and `search.db` own the one disposable full-text serving index;
- session search and recall have one lexical mode; the old embedding,
  semantic, hybrid, and automatic-fallback serving paths are removed;
- the canonical event API is cursor-based;
- CLI, MCP, web, and native clients consume the same machine contracts;
- historical raw provider evidence remains readable, but there are no runtime
  legacy-store branches, offset adapters, retired recall workers, or duplicate
  continuity routes.

The old `retrieval.db` recall design and embedding-based search plane are
superseded. This epic removes their routes, modes, jobs, subprocesses, caches,
configuration, tests, documentation, and startup wiring rather than retaining
dormant compatibility planes.

The same convergence rule applies to the rest of the Runtime Host: remove the
`live_catalog_enabled()` switch and make the catalog/object architecture
unconditional across routers and services. Version suffixes are also removed
from internal RPC names in one mechanical, repo-wide lockstep cutover. This is
not a search-only rename: cross-binary envelopes such as the Machine Agent ship
trace must change engine and host together, with no alias left behind. Numeric
schema-version fields may remain where they validate persisted data; parallel
runtime semantics may not.

One-time data conversion is acceptable when required to preserve raw session
history. Permanent dual reads, fallback stores, and compatibility shims are
not.

## Why This Is Launch-Critical

The launch loop is import or start sessions, find them fast, and steer them
later. A session archive that cannot be reliably found or cheaply understood
does not satisfy that loop.

On 2026-07-19, a real cross-agent handoff was attempted from the final sentence
of a 525-event Claude session. Longhouse retained the exact source session and
all of its events, but the normal recovery path failed:

| Observation | Result |
| --- | --- |
| `longhouse recall` with exact closing text | `503 search_unavailable` |
| Recall with a unique commit SHA or `61m51s` | `503 search_unavailable` |
| Runtime `/api/readyz` and `/api/health` | Healthy |
| `searchd-status.json` | `ready=true`, `status=running` |
| Lexical session search | Intermittently irrelevant results, then the same 503 |
| `longhouse sessions events --offset 100` | 400; the canonical store requires a cursor |
| CLI cursor option | Missing |
| Session-level generated title | Present |
| Session-level summary | Null |
| Startup continuity route | `archive_route_unavailable` |
| Marked compaction summaries | Zero |

Recovery succeeded only by listing the recent wall, guessing the session from
chronology and title, then directly cursor-paginating six API pages.

The recovered corpus also validates the expected compression opportunity:

| Material | Amount |
| --- | ---: |
| Total events | 525 |
| Total textual payload | 268,575 characters |
| Genuine human turns | 14 |
| Injected events represented as `role=user` | 26 |
| Assistant events | 278 |
| Assistant events containing text | 92 |
| Genuine human plus assistant text | 43,651 characters, about 16% |
| Tool output | 178,341 characters |

A mechanically clean conversation slice was small enough for a receiving
agent to understand the entire task while preserving raw drill-down for claims
that needed verification.

## Product Outcomes

1. An exact phrase, commit SHA, file path, command flag, or natural-language
   description finds the right session when the evidence exists.
2. Search degradation is explicit in health, APIs, and CLIs. A ping-only green
   sidecar cannot mask failed queries.
3. A receiving agent can obtain the useful conversation without tool-output
   bulk, injected context, empty tool-call shells, or provider scaffolding.
4. Every projected item remains linked to canonical raw evidence.
5. Large sessions use bounded progressive disclosure rather than whole-trace
   hydration or eager lossy summarization.
6. CLI, HTTP, MCP, and future agent clients share one machine-facing contract.
7. Context recovery is paired with existing `session.capabilities`, so the
   receiving agent knows whether it can steer live, reattach on the host, or
   only start a new session with recovered evidence.

## First Principles

### The archive is the source of truth

Longhouse owns raw session history. Search indexes, titles, and conversation
projections are derived and rebuildable. Losing a derived index may degrade
discovery, but it must never make the archive appear lost or silently return
fabricated completeness.

### Infrastructure handles mechanics; the receiving model handles judgment

The Runtime Host should deterministically handle storage, pagination, source
identity, provenance, budgets, filtering of structurally known scaffolding,
and honest degradation. The receiving model decides which decisions mattered,
what the task means now, and what to do next.

Do not build a rule engine that tries to identify the “important” assistant
messages. Give the model clean messages, timestamps, source references, and
bounded raw evidence.

### Do not insert an LLM just to serve another LLM

The default handoff path is not a background-generated prose summary. A
powerful receiving agent reading a compact evidence projection is the summary.
Only compress with a model when the selected evidence still exceeds the
consumer's explicit budget, and do it as late as possible. Any such compression
is revision-keyed, source-linked, visibly derived, and never authoritative.

### Progressive disclosure beats one giant response

The normal path has three levels:

1. Search result: identity, title, dates, project, provider, and exact evidence
   snippet.
2. Conversation evidence: clean ordered messages and typed actions, with raw
   tool references rather than full outputs by default.
3. Forensic evidence: selected raw events, tool results, source lines, and all
   branches when requested.

### Provenance must be explicit

`role=user` does not prove that a human typed the text. Provider skill
injections, Longhouse monitor notifications, synthetic control markers, and
context blocks need a provider-neutral projected kind. Use structured source
evidence where it exists. If old retained rows need classification, convert
them once into the canonical shape; do not keep request-time format fallbacks or
broad textual heuristics.

## Non-Goals

- Building graph memory, a knowledge graph, or a second session archive.
- Making `session.summary` a canonical handoff document.
- Adding a Longhouse-hosted LLM compression layer for agent evidence; receiving
  agents may compress within their own context when needed.
- Copying every tool request and response into the default context.
- Adding a generic task tracker or repo-local handoff system.
- Replacing provider compaction behavior or depending on compaction summaries.
- Adding Kubernetes, distributed databases, or unrelated scaling machinery.
- Hiding search failure behind a fallback or an empty result.
- Preserving pre-launch search/event compatibility branches after the canonical
  path ships.

## Contract 1: Search And Recall Availability

### Query readiness, not process readiness

Search health must distinguish:

- process reachable;
- schema compatible;
- projection freshness known;
- representative query succeeds within the interactive timeout.

`search.ping` remains useful process evidence, but a bounded query probe is
the serving-readiness check. Machine health exposes at least:

```json
{
  "search": {
    "status": "ready|degraded|rebuilding|unavailable",
    "last_success_at": "...",
    "projection_lag": 0,
    "reason": null
  }
}
```

Hot ingest and control readiness may stay green while search is degraded, but
the overall health response and user-facing status must disclose that a
launch-critical support capability is impaired.

### Search response honesty

Successful search responses include:

- `index_revision` or equivalent projection identity;
- match kind and source locator;
- an exact evidence snippet;
- whether results are complete for the requested time window.

An unavailable full-text index must not return `200` with an empty list. Before
launch there is no second metadata-search implementation or silent fallback:
return a typed failure with the underlying reason and repair the one index. The
same rule applies during first-boot projection and rebuilds: `rebuilding` is a
typed unavailable state, not an empty corpus.

Unqueried timeline and session-list reads remain catalog-only and available
when searchd is degraded. Searchd is the single dependency only for queries;
its failure must not hide ordinary chronological session access.

### Query behavior

The quality floor covers:

- exact quoted prose;
- commit SHAs;
- dotted filenames such as `search.db`;
- slash paths;
- snake_case identifiers;
- flags such as `--no-verify`;
- ordinary natural-language descriptions.

Escaping and tokenization are server mechanics. A user's punctuation must not
turn a normal query into a sidecar failure.

### Recall is evidence retrieval, not a separate truth plane

`/api/agents/recall` may rank smaller records than session search, but both
surfaces are served by searchd and share the same serving status, source
locators, visibility rules, and degradation vocabulary. CLI documentation must
not claim that lexical search uses the primary store.

## Contract 2: Cursor-Native Event Access

Canonical event order is cursor-based. The API already exposes opaque cursors,
generation identity, and `has_more`; the CLI and MCP adapters must consume that
contract consistently.

The event response includes:

```json
{
  "events": [],
  "next_cursor": "opaque-or-null",
  "has_more": true,
  "total": 525,
  "count_is_estimate": false
}
```

Requirements:

- Add `--cursor` to `longhouse sessions events`.
- Add a bounded `--all` mode that follows cursors and reports its event/byte
  ceiling before truncation.
- Remove `--offset` from the event surface; cursor traversal is the only
  contract.
- Define filter semantics explicitly. Prefer counting returned filtered rows;
  if the store cannot compute an exact filtered total without scanning, omit it
  or mark the count as an estimate. A request must never return 51 rows from a
  limit of 100 without explaining that the filter applied within a raw page.
- Update MCP event/detail tools to use cursors rather than advertising broken
  offset pagination.
- Keep cursors opaque and use the existing `generation_id` seam so a rebuild
  fails honestly instead of duplicating or skipping events.

## Contract 3: Typed Conversation Projection

Add a read-time provider-neutral projection over canonical events. Do not add a
new durable table in the first slice.

Projected items are siblings rather than overloaded chat roles:

```json
{
  "kind": "message|action|context|tool_ref|system",
  "source_ref": {
    "session_id": "...",
    "event_id": "...",
    "cursor": "opaque-event-position",
    "generation_id": "...",
    "raw_locator": "opaque-or-null"
  },
  "timestamp": "...",
  "message": {
    "author": "human|assistant|provider|longhouse",
    "role": "user|assistant",
    "text": "..."
  },
  "context": {
    "context_kind": "skill|startup_continuity|monitor_notification|other"
  },
  "tool_ref": {
    "tool_name": "...",
    "status": "completed|failed|unknown",
    "input_preview": "...",
    "output_preview": "..."
  }
}
```

Only the fields relevant to the selected `kind` are present.

### Default conversation view

The default view includes:

- genuine human-authored messages;
- non-empty assistant text;
- typed session actions that affect interpretation;
- provider compaction summaries when present, clearly marked as provider
  context rather than human speech;
- compact tool references when they connect assistant text to evidence.

It excludes by default:

- full tool output;
- empty assistant tool-call shells;
- file-history snapshots;
- skill bodies and startup context repeated as user messages;
- Longhouse monitor/task notifications represented as human speech;
- known synthetic provider control markers.

Excluded evidence remains accessible in forensic mode.

### Classification mechanics

Prefer, in order:

1. structured provider event type;
2. Longhouse-owned insertion metadata;
3. canonical fields written by a one-time migration for retained history;
4. `kind=message`, preserving uncertainty rather than guessing.

Do not grow a general regex list for deciding authorship or importance.

The current session-action projection is only a narrow Codex interruption
precedent, not a general provenance layer. Inventory retained evidence first,
then extend the same typed-item seam where the source can support it rather than
creating a competing classifier.

The inventory must also resolve two existing projection remnants:

- delete the old `context_mode=active_context` boundary machinery or adopt its
  canonical structured fields into this one projector;
- keep `CleanTranscriptEvent` and `iter_clean_transcript_events` only if they
  become the canonical mechanical projection seam after retrieval and
  embedding callers are deleted; otherwise remove them too.

## Contract 4: Agent Evidence Pack

Expose the conversation projection as a bounded agent-facing evidence pack,
using the canonical route and command:

```text
GET /api/agents/sessions/{session_id}/context
longhouse sessions context <session-id>
```

`context` is the canonical noun: the server supplies evidence to the receiving
agent without claiming to have authored the handoff judgment.

The route uses the same owner-bound machine-token authorization as canonical
session search and rejects cross-owner session access.

The response contains:

- session identity and capability metadata;
- ordered clean conversation items;
- compact tool references;
- explicit omitted counts and byte totals by kind;
- `next_cursor` for more evidence;
- source references on every item;
- canonical `generation_id`;
- canonical `session.capabilities` describing live control, host reattach, or
  search-only continuation options.

`next_cursor` is the raw event cursor bound to `generation_id`, not a projected
item ordinal. Filtering must not create a second pagination identity that can
shift when live events arrive.

It does not contain a server-authored “goal,” “decision,” or “next step” field
by default. Those are judgments for the receiving agent. The CLI may render a
small Markdown preamble explaining the evidence shape, followed by the source
material.

### Budget behavior

Consumers may specify an event or byte budget. The default is 64 KiB of
projected message text plus bounded metadata; the response reports the applied
budget. Selection is mechanical and disclosed:

1. keep the first genuine human message;
2. keep genuine human messages in order;
3. keep their adjacent assistant text while budget permits;
4. keep the recent tail;
5. replace bulky tool bodies with references;
6. return continuation cursors and omission counts.

If genuine conversation still exceeds the budget, do not silently summarize
it. Return a bounded head/tail plus continuation ranges. A receiving agent can
read another range or compress the evidence within its own context.

If one item alone exceeds the budget, return a source-linked truncated item
with `truncated=true`, its original byte count, and an event cursor for fetching
the full evidence. Never silently drop the opening human message.

## Contract 5: Existing Summary And Startup Surfaces

`summary_title` remains useful human navigation metadata. `anchor_title`
remains a stable card headline. Neither replaces evidence retrieval.

The current background `session.summary` and startup-continuity lab depend on
the retired archive-backed path. Split title generation into a title-only lane,
then remove persisted prose summaries and their revision/status/lock machinery.
Update or delete every consumer, including timeline summary status, hot-card
derived revisions, response projection, archive export, demo data, CLI session
detail, search snippet fallbacks, embeddings, and startup-continuity hooks.

Delete the startup-continuity lab directory and hook markers. Keep only
`summary_title` and `anchor_title` enrichment used by human navigation. Do not
repair prose summaries when the evidence-pack path serves agents better.

## Machine API And CLI Shape

The canonical machine surface should converge on these primitives:

| Need | HTTP | CLI |
| --- | --- | --- |
| Search sessions | `GET /api/agents/sessions?query=` | `longhouse sessions search` |
| Retrieve ranked evidence | `GET /api/agents/recall` | `longhouse recall --json` |
| Inspect one session | `GET /api/agents/sessions/{id}` | `longhouse sessions get` |
| Read typed conversation | `GET /api/agents/sessions/{id}/context` | `longhouse sessions context` |
| Read forensic events | `GET /api/agents/sessions/{id}/events` | `longhouse sessions events` |
| Inspect search health | machine health response | `longhouse status --verbose` |

Avoid adding a second MCP-only continuity abstraction. MCP wraps the same
routes and cursors.

## Delivery Plan

### Immediate Incident: Repair Live Search Before Epic Gating

- Add a deterministic repro for “ping succeeds, representative query fails.”
- Preserve the real exception/reason from the search query RPC through logs and
  operator health instead of collapsing every failure to `search_unavailable`.
- Measure the one-second interactive deadline and projector/read contention;
  fix the demonstrated timeout, locking, or query issue rather than guessing.
- Add a query-serving health probe and hosted smoke.
- Repair query normalization for quoted text, dotted filenames, flags, SHAs,
  and punctuation-only input as a separate quality defect.
- Verify exact phrase, SHA, dotted filename, and natural-language queries
  against a synthetic fixture that reproduces the measured session shape.

Exit: hosted dogfood recall works while the index is healthy, and an intentionally
broken query path is visibly degraded even when the process still pings.

### Slice 1: Converge The Repository On One Storage And Search Architecture

- Delete the retired retrieval-index services, jobs, subprocess, lifecycle
  wiring, index routes, tests, and implementation-plan document.
- Remove `live_catalog_enabled()` and every alternate-store branch across
  routers, services, dependencies, serializers, runtime/control paths, health,
  ingest, attachments, machines, users, permissions, timeline, session detail,
  and search. Catalogd plus the object/event store become unconditional.
- Delete `/api/agents/sessions/semantic`, `mode=semantic|hybrid|auto`, silent
  recall fallthrough, `session_hybrid_search`, embedding serving/cache code,
  embedding backfills/config, and their tests. Searchd is the only query plane.
- Split stable title generation from `session.summary`, then delete prose
  summary persistence, workers, consumers, and the startup-continuity lab.
- Remove the timeline compatibility response branch and client-side card
  reshaping; query and chronological results use one response model.
- Rename all version-suffixed internal RPCs repo-wide without aliases. Change
  cross-binary Machine Agent/Runtime Host envelopes in lockstep and verify both
  sides before dogfood refresh and hosted rollout.
- Delete offset emulation that fetches `limit + offset` then slices in routers.
- Decide whether the clean-transcript helpers and active-context boundary code
  become part of the one context projector or are deleted.
- Delete dead configuration, environment flags, generated tool contracts, docs,
  profiling scripts, demo fixtures, and tests that mention the removed paths.
- Use an explicit one-time migration only if retained user history needs a data
  shape conversion; do not add dual reads.

Exit: there is one search index, one recall serving path, one cursor event path,
and no runtime branch that selects an old store or contract. Mechanical audits
find no `live_catalog_enabled`, retrieval-index import, semantic/hybrid mode,
embedding serving path, offset emulation, startup-continuity hook, prose-summary
consumer, timeline compatibility response, or version-suffixed RPC name.

### Slice 2: Align Event Pagination Everywhere

- Treat the API's existing cursor and `generation_id` fields as canonical.
- Fix CLI `--cursor` and bounded `--all` behavior.
- Fix MCP event/detail tools and their help text.
- Remove offset parameters from the canonical API, CLI, generated tool schema,
  and MCP surface rather than translating them.
- Implement and document one honest filtered-page contract.
- Add one set of canonical contract tests.

Exit: a 525-event session can be traversed completely through CLI and MCP
without custom HTTP code, duplicates, or skipped rows.

### Slice 3: Inventory Provenance Before Designing The Projection

- Inventory structured provider and Longhouse-owned provenance already retained
  in raw events.
- Record which injected contexts, monitor events, actions, tool calls, and
  provider summaries can be identified mechanically for the reference Claude
  session and current provider fixtures.
- Identify missing ingest fields that Longhouse itself can stamp at creation
  time; do not infer them later from prose.
- Decide whether active-context boundary fields and clean-transcript helpers
  become the one projection seam or are deleted.
- Decide the smallest provider-neutral item kinds supported by evidence.

Exit: the projection vocabulary is justified by real retained fields, and
unknown shapes have an explicit fail-open representation.

### Slice 4: Ship Claude-First Conversation Context

- Define shared projection models and read-time classifier seams.
- Extend the existing typed-item/action seam where evidence supports it.
- Implement Claude against a synthetic fixture reproducing the real failure's
  measured shape. Add other providers only when a fixture demonstrates a
  distinct structured shape.
- Preserve unknown inputs as messages rather than inventing authorship.
- Add the canonical session-context route with cursor, budget, omission,
  generation, ownership, and capability fields.
- Add JSON and readable Markdown CLI output.
- Bind source references to existing event/cursor/generation identity.
- Add explicit truncation and continuation behavior.
- Generate the MCP wrapper through `schemas/tools.yml` rather than hand-writing
  a separate contract.

Exit: the reference session projects 14 genuine human turns, 92 non-empty
assistant text events, typed injected context, and tool references; a new agent
can recover its initial request, decisions, commits, current state, and open
host-registry thread from no more than 20% of that reference session's raw
textual payload, with all 525 forensic events one request away.

### Slice 5: Quality Evaluation And Documentation

- Add a labeled synthetic recall fixture set covering exact facts, unique
  artifacts, paraphrased intent, and noisy tool traces.
- Measure recall@k and MRR for discovery.
- Add handoff questions with source-backed expected facts: initial objective,
  material decisions, shipped changes, unresolved next move, and known risks.
- Compare agent answers from the evidence pack against full-transcript answers,
  recording evidence bytes and unsupported claims.
- Document the final recovery ladder in CLI and machine-surface docs.

Exit: the evidence pack preserves decision-relevant understanding while using a
small fraction of raw payload, and every answer can be traced to source events.

## Test And Quality Gates

### Availability

- Search query failure while ping remains green produces degraded search health.
- Search outage never returns a false empty-success response.
- Rebuild state and projection lag are visible.

### Retrieval quality

- Exact final prose finds the source session.
- `e8aeded`, `61m51s`, `search.db`, `objects-v2`, and a paraphrase of “complete
  the backup coverage” rank the reference session in the expected top-k.
- Search results include the supporting snippet and source locator.

### Projection fidelity

- Human-authored messages are not confused with injected context.
- Empty assistant tool-call shells disappear from normal conversation.
- Tool evidence remains reachable by source reference.
- Typed actions and provider compaction markers do not become human speech.
- Unknown provider shapes fail open to visible evidence, not silent deletion.

### Pagination

- Cursor traversal returns every event exactly once.
- Filters work across cursor page boundaries.
- Rebuild-invalidated cursors fail with a typed retryable error.
- CLI and MCP can traverse the full reference session.

### Handoff usefulness

- A receiving agent answers the labeled task-state questions with source
  support and no material unsupported claims.
- The default evidence pack is at most 20% of the reference raw textual payload.
- Omissions are counted and visible; “complete” is never implied after
  truncation.

## Cutover

- Ship the incident repair first, then remove competing paths before introducing
  the new projection.
- Change all in-repo clients in the same commit series as each canonical API
  change; there is no compatibility window.
- Add the context route without changing default forensic event visibility.
- Introduce provider projections behind shared fixtures, not provider-specific
  client logic.
- Backfill only structured metadata that can be derived mechanically from raw
  evidence. Do not run a corpus-wide LLM summarization job.
- Preserve historical raw session evidence. Where precise provenance is absent,
  show an unknown/undifferentiated item rather than retaining an old runtime
  parser or store.

## Resolved Product Decisions

1. The route and command noun is `context`; it describes evidence supplied to a
   receiving agent without promising a server-authored handoff judgment.
2. There is no emergency fallback search implementation before launch. Failure
   is typed and visible.
3. Filtered totals are optional; returned-page semantics and estimate state are
   explicit.
4. LLM compression remains a receiving-agent prompt pattern, not a Longhouse
   service.
5. Provenance inventory precedes the final projection vocabulary.

## Definition Of Done

- Search and recall are query-health-aware, source-linked, and honest under
  degradation.
- CLI and MCP can traverse canonical cursor-based sessions completely.
- The canonical machine API exposes a clean typed conversation/evidence view.
- Another capable agent can understand and continue a large prior session
  without ingesting raw tool bulk or trusting a lossy canonical summary.
- A synthetic regression fixture reproduces the measured 525-event shape,
  payload size, turn distribution, injected-context mix, and tool bulk without
  committing any real session content.
- Machine-surface and CLI documentation describe one recovery ladder with no
  false primary-store fallback claims.
- Search, recall, event reads, and context contain no old-store branches,
  offset compatibility, duplicate workers, or pre-launch versioned semantics.
