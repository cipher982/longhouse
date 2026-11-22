---
name: longhouse-concepts
description: Where an existing Longhouse check, matrix, gate or proof lives, and whether one exists before you build another. Covers the provider signal parity matrix and transcript census, session-state facts, tool tiers, terminal truth gate, release rings and ring lock, released-engine compatibility (engine-compat), perf proofs, live QA producers and the recall eval. Use when asked how to check, measure or find something in Longhouse, and before proposing a new census, matrix, gate, tracker, dashboard or harness.
---

# Longhouse concepts

Longhouse already has most of the measuring and gating systems an agent is
tempted to build. Read `~/git/zerg/control-plane/docs/concepts-index.md` before
you write a plan that adds one. It lists each system with its make target, spec
or skill, grouped by area (provider transcripts, session state, shipping, provider
checks, iOS, testing, performance, hosted, agents).

The index is generated from `control-plane/scripts/workspace_index/concepts.toml`.
To add or change an entry, edit that file and run `make workspace-index` from
`control-plane/`; `make validate-workspace-index` checks that every cited path,
make target, spec and skill exists. Register a new system there when you build
it, so the next agent finds it.
