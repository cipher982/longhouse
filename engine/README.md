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

For Claude Console, an unbound wake response that idles is not kept alive for a
later runtime bind: buffered projection is left to the native transcript and the
invocation follows its parked/closed registry state. A late wake is cancelled
as `wake_target_gone`; a user turn arriving first binds the response and queues
input on the same process.
OMP Helm observes the stock owner-scoped async manager and preserves native
progress separately from child archive counters. Native handles never become
child session IDs; navigation requires exact provider-authored lineage.
Recent terminal jobs retain their status and native end time separately from
the active registry: they never add active work or a flame root. A newer
complete registry is authoritative over older lifecycle edges; callbacks do
not become permanent client-side completion tombstones.

Test: `make test-engine`. Install locally: `make install-engine`.
