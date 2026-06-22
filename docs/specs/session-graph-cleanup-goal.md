# Session Graph Cleanup Goal

Status: active implementation goal
Owner: Longhouse session core
Created: 2026-06-21

## Goal

Make provider lineage support boring enough that new upstream features do not
spread provider-specific decisions across ingest, routes, archive export,
timeline projection, and release-watch code.

This cleanup covers the first three simplification tracks:

1. Finish session graph consolidation.
2. Begin splitting `AgentsStore` along real responsibilities.
3. Move provider behavior toward pure evidence emission.

## First Principles

- Provider adapters emit evidence, never product decisions.
- The shared resolver owns classification.
- `session_edges` owns durable lineage truth.
- Aliases are lookup aids and labels, not semantic truth.
- Projection modules own API, UI, workflow, and archive shape.
- Store classes should orchestrate narrow services, not become the services.
- Capability/projection tests should be semantic and table-driven wherever the
  behavior is provider-neutral.

## Track 1: Graph Consolidation

`session_edges` is the readable truth for lineage: task child, fork, and
unknown parentage. Thread aliases remain for provider lookup compatibility,
source labels, workflow labels, and relink evidence.

Success criteria:

- No route decides child/fork/link behavior from raw aliases.
- Archive export and reclaim discover child archive owners through graph
  projection.
- Workflow compatibility endpoints read through the graph/projection layer.
- API tests prove parent task children, visible forks, linked unknown parentage,
  and orphan relink behavior.

## Track 2: Store Split

`AgentsStore` remains the compatibility facade while responsibilities move into
small modules. The first split is graph writing:

- `session_graph_writes.py`: primary threads, child threads, aliases, edges,
  provider-session lookup.
- `kernel_writes.py`: launch attempts, runs, and control connections.
- `session_graph_projection.py`: graph/workflow/archive read projections.

Success criteria:

- Production ingest/backfill imports graph helpers from
  `session_graph_writes.py`.
- Existing `kernel_writes` imports keep old callers working during migration.
- Focused ingest/relink tests prove behavior did not change.
- Later splits should follow the same pattern: extract a narrow service, keep
  compatibility at the old facade, then migrate callers gradually.

## Track 3: Evidence-First Providers

Provider-specific parsing should normalize facts into evidence structs such as
`ObservedSession` and `ObservedLineageEdge`. Product behavior comes after that,
through the resolver.

Success criteria:

- Resolver fixtures cover provider-neutral semantics:
  task child, fork, unknown parentage, orphan relink, agent switch, async prompt.
- OpenCode ingest uses normalized lineage evidence before choosing child/fork
  projection behavior.
- Provider action coverage records supported, read-only, unsupported, and
  unknown states by deriving from provider contracts plus executable proof.
- Release-watch harness emits capability-level evidence rather than one blob
  verdict per provider.

## Verification Bar

Before a tranche is considered done:

- Run focused semantic tests for resolver, graph ingest, archive ownership, and
  workflow projection.
- Run API-level tests for `/api/agents/*` or timeline mirrors touched by the
  tranche.
- Run provider/release harness tests when capability or provider evidence
  changes.
- Run `make test` for backend changes.
- Run `make test-engine` when parser or engine ingest changes.
- Run E2E only when browser-visible timeline/session behavior changes; use
  `make test-e2e-core` first, then full `make test-e2e` if the core path is
  affected broadly.

Timing-only flakes must be rerun by exact test name and reported explicitly.
