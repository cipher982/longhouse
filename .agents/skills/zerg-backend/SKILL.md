---
name: zerg-backend
description: Edit Longhouse server code under server/zerg (routes, models, catalog and session state, migrations, auth, generated SDK). Use before adding an API route, a column or table, a high-frequency write path, or code that reads served session state, so the conventions the code does not announce are in hand.
---

# Longhouse backend conventions

Facts the code answers (limits, minimum versions, defaults) are not repeated
here; read the code. These are the conventions that are easy to break without a
test noticing at once.

- **Routes:** every `/api/*` route lives on `api_app`, not `app`. `/api/agents/*`
  is the canonical machine surface; the browser and MCP are clients of it.
- **Generated code:** never edit `server/zerg/generated/` or
  `web/src/generated/`; run `make generate-sdk`. A new route or response field
  fails `make validate-sdk` until you regenerate, so regenerate in the same change.
- **Models:** agent infrastructure models use `AgentsBase`, not `Base`.
  High-frequency presence, heartbeat, ingest, runtime and runner writes go
  through `WriteSerializer` or catalogd's single writer.
- **Schema changes:** a plain nullable or `server_default` column auto-adds at
  startup. Defaults, backfills, indexes, renames, drops and rebuilds need a
  migration. Heavy historical rewrites are explicit operator commands
  (`zerg-hosted-debug`, "Heavy migrations"), never startup work.
- **Served state:** session state (idle, working, needs input) and command
  authority come from catalogd fact snapshots (`live_catalog_timeline`,
  `session_state_facts_projector`). `session_runtime_state` is legacy evidence,
  not authority. In local-health, `control_path`, `liveness_model` and `state`
  are independent, and an ordinary provider `needs_user` is Idle unless an
  explicit structured interaction exists.
- **Evidence, not inference:** a run row records an attempt. Liveness, readiness,
  control and run lifecycle are independent; missing or expired evidence reads
  as unknown, never as ended, idle or disconnected
  (`control-plane/docs/specs/managed-run-ownership-horizon.md`).
- **No silent fallbacks or mode switches:** behavior and capability gates are
  explicit.
- **Config:** `config/models.json` owns model and embedding config.
- **Auth:** off in dev (`AUTH_DISABLED=1`). Browser auth uses the
  `longhouse_session` cookie; `/api/agents/*` uses `X-Agents-Token`.
- **Data movement:** every transfer path keeps bandwidth x RTT in flight and
  compresses what compresses; stop-and-wait or an uncompressed stream is a
  defect. Measure wire throughput before and after
  (`control-plane/docs/specs/transcript-shipping-throughput.md`).
- **Limits:** before raising a cap, timeout, grace period or budget, measure what
  grew and why, and write down the arithmetic for the new value and its margin.
- **Core database:** SQLite is the only one. Hosted account, billing and
  provisioning live in the control plane.

Tests for the backend: `zerg-testing`. Existing systems before you build a new
one: `longhouse-concepts`.
