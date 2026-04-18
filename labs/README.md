# Labs

This directory holds Longhouse side bets — pieces that work, but are not part of
the launch product.

## Contract

Anything under `labs/`:

- is **not installed by default** by `longhouse connect --install` or `Longhouse.app`
- is **not advertised** on the landing page, README, or docs/specs canon
- is **opt-in** via an explicit `longhouse labs enable <name>` step or a dedicated
  script inside the lab's own directory
- **may be removed without deprecation** if it stops earning its keep

If something under `labs/` starts being relied on by the launch product, promote
it out. Until then, treat it as experimental.

## Current labs

- [startup-continuity](startup-continuity/README.md) — inject a small
  project-scoped recap of recent sessions on provider `SessionStart` so new
  sessions start with cross-provider memory.
