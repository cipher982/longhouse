# Platform-Native State Layout

Status: Deferred Tier 2
Last updated: 2026-05-27

Longhouse currently keeps local durable state under the Longhouse home
(`~/.longhouse` by default). That is acceptable for launch. The immediate
prelaunch requirement is that every process agrees on the same Longhouse home,
including `LONGHOUSE_HOME` and provider-home-derived scratch layouts.

## Current Contract

- Durable machine state, shipper DBs, outboxes, config, managed-session staging,
  and bridge state live under the Longhouse home.
- Codex bridge readers and writers use the same `managed-local/codex-bridge`
  directory resolved from the Longhouse home.
- Legacy `~/.claude/managed-local/codex-bridge` files are stale pre-migration
  state. They may be reported by doctor surfaces, but they are not active
  liveness truth.

## Deferred Split

A later platform-native split should separate:

- config: platform config dir
- durable data: platform application data dir
- logs/state: platform logs or state dir
- runtime: a short, private runtime dir for PIDs, sockets, locks, and live
  bridge credentials
- cache: platform cache dir

Do not land that as a simple helper rename. It is a compatibility migration
because active bridge sessions have state files, locks, sockets, and process
coordinates in the current tree.

## Constraints For Tier 2

- Explicit `LONGHOUSE_HOME` or test `base_dir` must dominate isolation. Runtime
  paths for isolated homes must stay inside that isolated tree.
- macOS socket paths must stay under the Unix `sun_path` limit. `$TMPDIR` can be
  too long; measure before choosing a default runtime root.
- Runtime directories containing live bridge credentials must be private
  (`0700` parents, `0600` files).
- Do not read old and new bridge state as equal active truth. If a transition
  needs old-state visibility, expose it as legacy/stale evidence with explicit
  user repair or cleanup guidance.
