---
name: longhouse-concepts
description: Find the Longhouse system that already exists before proposing or building a census, matrix, gate, tracker, dashboard, harness, ledger or similar. Use when a plan adds a new checker, coverage table, QA producer, promotion or perf proof, so you extend what is there instead of duplicating it.
---

# Longhouse concepts

Longhouse already has most of the measuring and gating systems an agent is
tempted to build. Read `~/git/zerg/control-plane/docs/concepts-index.md` before
you write a plan that adds one. It lists each system with its make target, spec
or skill, grouped by area (provider transcripts, session state, shipping, provider
factory, iOS, testing, performance, hosted, agents).

The index is generated from `control-plane/scripts/workspace_index/concepts.toml`.
To add or change an entry, edit that file and run `make workspace-index` from
`control-plane/`; `make validate-workspace-index` checks that every cited path,
make target, spec and skill exists. Register a new system there when you build
it, so the next agent finds it.
