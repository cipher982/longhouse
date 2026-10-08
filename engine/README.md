# engine

The Rust Machine Agent. It builds two binaries: `longhouse-engine`
(`src/main.rs`: discovers provider transcripts, ships them from per-source storage-v2 cursors with
retry, heartbeats, runs the control channel) and the native `longhouse` CLI
(`src/longhouse.rs`: auth, local health, machine repair and scope, uninstall,
and managed provider launches such as `longhouse codex`). Where it sits in the
system: the Code map in [`ARCHITECTURE.md`](../ARCHITECTURE.md#code-map).

- `src/pipeline/` parses and compresses transcripts; `src/shipping/` sends them.
- `src/control_channel/` is the control WebSocket: `connection` (connect, reconnect,
  heartbeat frames), `dispatch` (command frames and receipts around `execute_command`),
  `turn_start` (Console turns), `receipts` (durable command receipts) and
  `capabilities` (contract manifest and provider readiness).
- `src/state/` is the local SQLite state: source epochs and pending envelopes, file offsets, session phase.
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

Current status is asserted only for the exact execution owner. A response's
exact terminal event is persisted atomically with its run claim before outbox
handoff; failed handoffs replay that retained event after restart. Its status
retires only after confirmed durable handoff, without deleting pending work or
history; delayed callbacks cannot remove or reclaim a successor's status.
Codex restart closure uses the stateless `invocation_closed` contract, retained
separately and replayed by the daemon without rewriting the response outcome.
Claude, Codex and OMP publish restart closure and the user's Stop of a parked
invocation (`session.invocation.close`) the same way, naming the pending work
it stopped and clearing the delegation snapshot. Stop signals the process
group only after a recorded member (pid, birth time, pgid) proves it ours; once
provider input is closed the invocation is closed, and a survivor or an
unverifiable group is reported in the reply's `error_note` for the janitor.
Tentative close intent can roll back when shutdown survives; a retained
closing event is final. The first exact terminal payload takes precedence
over earlier thin metadata; conflicting later exact payloads fail explicitly.
Unknown ownership never renews remote liveness; its reconstructable observation
expires after 24 hours with an exact-version guard, without declaring execution
ended. Runtime lifecycle records have independent admission and cooldowns from
replaceable observations.
Helm and Console ownership are revalidated together on the existing managed
scan cadence. New exact owners resolve on the one-second status cadence through
bounded, batched PID probes; stale or failed evidence remains unknown.

Fresh OMP Helm and Console use native creation, not resume. OMP may report its
exact session path before writing a JSONL header: controls can be ready while
that source remains pending. Archive admission verifies the provider-written
header's identity and workspace; cold resume uses the validated retained file.

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
OMP Console runs the stock OMP RPC process persistently: each terminal
`agent_end` settles that response, while async jobs keep the invocation parked
until OMP starts a follow-up wake or the user sends another prompt to the same
process. `session_settled` closes the invocation after pending work drains.
Pi Console also uses RPC mode but closes at `agent_settled`; upstream Pi has no
background-job lifecycle to support the OMP wake scenarios.

Codex Console keeps its app-server worker across turns while commandExecution
items remain. It reconciles `thread/backgroundTerminals/list` with in-progress
items and turns delayed `item/completed` notifications into Longhouse wake
turns. Wake input is explicitly Longhouse-authored and carries the finished
command and exit code plus at most 40 lines / 4 KB of output. Machine Agent
recovery kills only the recorded process group because stdio cannot be reattached.
An unbound wake expires after 10 minutes; an intervening user turn supersedes
it on the same worker.

Test: `make test-engine`. Install locally: `make install-engine`.
