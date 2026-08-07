# Longhouse docs

Public documentation and operational runbooks for Longhouse. Detailed private
architecture and control-plane specifications are maintained outside this
repository; this directory must not link to paths that are absent from the
public checkout.

**Start with the top-level docs instead:**

- [`../VISION.md`](../VISION.md) — product thesis and invariants (the north star)
- [`../ARCHITECTURE.md`](../ARCHITECTURE.md) — system map + glossary
- [`../CONTRIBUTING.md`](../CONTRIBUTING.md) — dev setup, test tiers, codegen

## Public contracts and design context

- [`contracts/truth-plane.md`](contracts/truth-plane.md) — public truth-plane
  contract
- [`../ARCHITECTURE.md`](../ARCHITECTURE.md) — system map and terminology
- [`../VISION.md`](../VISION.md) — product thesis and invariants

Private implementation specifications are intentionally not mirrored here.
When working in the shared workspace, use the private control-plane spec index
as the routing map for those documents.

## Runbooks

Operational runbooks live in [`runbooks/`](runbooks/).

- [`runbooks/production-logging.md`](runbooks/production-logging.md) — production stdout retention and journal queries
