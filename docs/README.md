# Longhouse docs

Design specs and runbooks. Most files here are internal engineering RFCs
written during development — useful for understanding *why* something works the
way it does, but not required reading to use or contribute to Longhouse.

**Start with the top-level docs instead:**

- [`../VISION.md`](../VISION.md) — product thesis and invariants (the north star)
- [`../ARCHITECTURE.md`](../ARCHITECTURE.md) — system map + glossary
- [`../CONTRIBUTING.md`](../CONTRIBUTING.md) — dev setup, test tiers, codegen

## Canonical, current specs

These define contracts the rest of the system depends on — read these first if
you're touching the relevant area:

- [`specs/agents-machine-surface.md`](specs/agents-machine-surface.md) — the canonical `/api/agents/*` machine contract
- [`specs/machine-control-truth.md`](specs/machine-control-truth.md) — how control-path truth is resolved
- [`specs/runtime-display-contract.md`](specs/runtime-display-contract.md) — the server-authoritative runtime/liveness projection
- [`specs/model-capability-contract.md`](specs/model-capability-contract.md) — provider/model capability surface
- [`specs/macos-launch-product-shape.md`](specs/macos-launch-product-shape.md) — the macOS launch-product decision

## Historical design notes

The remaining files in `specs/` are point-in-time design records (session
identity, transcript planes, data-plane migration, propagation profiling,
provider state-compat, etc.). They reflect decisions as of their writing and
may not match current code line-for-line. Treat them as background, not as the
source of truth — the code and the canonical specs above are.

## Runbooks

Operational runbooks live in [`runbooks/`](runbooks/).
