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
- Obsolete `~/.claude/managed-local/{codex-bridge,opencode,antigravity}`
  directories contain only Longhouse-owned derived runtime state and may be
  removed by repair/install cleanup. Raw provider transcript/session logs are
  out of scope and must not be touched.

## Deferred Split

A later platform-native split should separate:

- config: platform config dir
- durable data: platform application data dir
- logs/state: platform logs or state dir
- runtime: a short, private runtime dir for PIDs, sockets, locks, and live
  bridge credentials
- cache: platform cache dir

Do not land that as a simple helper rename. It changes where active bridge
sessions keep state files, locks, sockets, and process coordinates.

Current trees in scope:

- `machine/`: device token, machine state, and state journal
- `agent/`: engine status, transcript wake socket, outboxes, shipper DB, and
  flight recorder unless `LONGHOUSE_ENGINE_FLIGHT_RECORDER_DIR` overrides it
- `agent/logs/`: engine logs
- `managed-local/codex-bridge/`: Codex bridge state, logs, locks, and sockets
- `managed-local/opencode/`: OpenCode runtime staging plus bridge session state
- `managed-local/antigravity/`: Antigravity runtime plugin staging
- `config.toml`: local runtime config

## Constraints For Tier 2

- Explicit `LONGHOUSE_HOME` or test `base_dir` must dominate isolation. Runtime
  paths for isolated homes must stay inside that isolated tree.
- macOS socket paths must stay under the Unix `sun_path` limit. `$TMPDIR` can be
  too long; measure before choosing a default runtime root.
- Runtime directories containing live bridge credentials must be private
  (`0700` parents, `0600` files).
- Do not read old and new bridge state as equal active truth. If a transition
  leaves obsolete Longhouse-owned telemetry behind, remove it through explicit
  repair/install cleanup instead of adding reporting surfaces or secondary
  truth channels.
- Engine path defaults are compiled into `longhouse-engine`. Any split that
  changes engine-visible paths needs a local engine refresh after merge, not
  just a Python CLI reinstall.
