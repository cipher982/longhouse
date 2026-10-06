# engine

The Rust Machine Agent. It builds two binaries: `longhouse-engine`
(`src/main.rs`: discovers provider transcripts, ships them with a spool and
retry, heartbeats, runs the control channel) and the native `longhouse` CLI
(`src/longhouse.rs`: auth, local health, machine repair and scope, uninstall,
and managed provider launches such as `longhouse codex`). Where it sits in the
system: the Code map in [`ARCHITECTURE.md`](../ARCHITECTURE.md#code-map).

- `src/pipeline/` parses and compresses transcripts; `src/shipping/` sends them.
- `src/state/` is the local SQLite state: spool, file offsets, session phase.
- `src/import_scope.rs` is what local history the machine may ship (a start
  time plus optional project folders, `machine/import-scope.json`). The engine
  enforces it where a source enters shipping (`discovery.rs`, the OpenCode
  session walk, the Claude presence hook; Cursor conversations are judged by
  their own start and folder in `cursor_store.rs`); `machine_scope.rs` is the
  `longhouse machine scope` command that writes it and `machine_uninstall.rs`
  is `longhouse uninstall` and the device-token revocation `auth --clear` uses.
- Provider modules are named by provider (`codex_*`, `claude_*`, `cursor_*`,
  `opencode_*`, `pi_*`, `omp_*`, `antigravity_*`); `managed_*` is the
  provider-neutral managed-session layer.
- `src/managed_phase_contract.rs` and `src/managed_identity_contract.rs` are
  generated; never edit them.

The runtime-event outbox measurement is collected from the same reads used to
build each delivery pass and is published as `runtime_event_outbox` in the
heartbeat and `engine-status.json`. A saturated pass marks its pending count as
a lower bound; an absent observation is unknown. `longhouse local-health`
degrades with `runtime_events_backlogged` when an observed event is at least
60s old. Sauron polls `GET /api/agents/machines/health` once per minute and
alerts at that same reported-age threshold.

Background evidence stays separate from parent activity. Claude registry
snapshots and exact child lifecycle callbacks retain independent clocks; a
completion callback can retire its matching task without renewing other work.

An unbound Claude Console wake response retains its buffered projection and
terminal outcome by `wake_id` for at most ten minutes. The invocation still
parks or closes according to pending work, so retained state alone never keeps
the provider process or conversation lock alive. A matching late wake bind
flushes the projection and terminal outcome; a later user turn, newer retained
wake, or expiry makes the old wake `wake_target_gone`.
OMP Helm observes the stock owner-scoped async manager and preserves native
progress separately from child archive counters. Native handles never become
child session IDs; navigation requires exact provider-authored lineage.
Recent terminal jobs retain their status and native end time separately from
the active registry: they never add active work or a flame root. A newer
complete registry is authoritative over older lifecycle edges; callbacks do
not become permanent client-side completion tombstones.

Codex Console keeps its app-server worker across turns while commandExecution
items remain. It reconciles `thread/backgroundTerminals/list` with in-progress
items and turns delayed `item/completed` notifications into Longhouse wake
turns. Wake input is explicitly Longhouse-authored and carries the finished
command and exit code plus at most 40 lines / 4 KB of output. Machine Agent
recovery kills only the recorded process group because stdio cannot be reattached.

Test: `make test-engine`. Install locally: `make install-engine`.
