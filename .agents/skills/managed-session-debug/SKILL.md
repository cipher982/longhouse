---
name: managed-session-debug
description: Diagnose slow or inconsistent managed Longhouse sessions by separating provider-loop latency, local hook/control-path health, hosted ingest lag, and runtime-state mismatches.
---

# Managed Session Debug

Use this when a managed Claude/Codex/Gemini session feels slow, appears stuck, shows the wrong lifecycle state, or disagrees between local CLI and hosted timeline.

## Fast Path

For "is transcript shipping live/slow?" questions, check `~/.longhouse/agent/engine-status.json` `ship_lanes.live` and `Shipped transcript` engine logs before static code reading.

1. Local truth:
   ```bash
   longhouse local-health --json | jq '.managed_sessions[]? | select(.session_id=="<session-id>")'
   ```

2. Provider transcript timing:
   ```bash
   scripts/ops/session-transcript-timing.mjs <session-id>
   ```

3. Hosted tenant truth:
   ```bash
   scripts/ops/hosted-session-debug.sh --subdomain <subdomain> --session <session-id> --limit 20 --json
   ```

4. Process and channel state for Claude:
   ```bash
   ps -axo pid,ppid,lstart,command | rg '<session-id>|claude-channel|longhouse-channel'
   test -f ~/.claude/channels/longhouse/sessions/<session-id>.json && jq . ~/.claude/channels/longhouse/sessions/<session-id>.json
   ```

## "How close to 1:1 was it?" — one command

For a post-hoc trace across every hop, run the trace tool instead of hand-writing
SQL. It prints the provider events, the engine's ships, the hosted chunk commits,
the activity-fact receipts, and the served payload, then names the slow hop:

```bash
python scripts/ops/session-realtime-trace.py --subdomain <subdomain> --session <session-id>
python scripts/ops/session-realtime-trace.py ... --since 2026-09-18T01:10:00Z --until 2026-09-18T01:35:00Z --json
```

Run it on the machine that owns the session for the first two sections; the rest
come from the Runtime Host over SSH (`--ssh`, default `zerg`). It is read-only and
needs no environment beyond a machine device token
(`~/.longhouse/machine/device-token`) for the served section.

### Read the result

- **provider -> hosted-commit lag** is `render_objects.created_at` minus the
  chunk's last transcript event. Steady state is ~1-7s. A single chunk far above
  that is a *write* gap: either the provider held the transcript line (Claude
  buffers while a dialog is open) or the engine's fsevent was late — check
  whether the engine shipped other paths normally in the same seconds.
- **engine queue/ship ms** comes from the path-job log
  (`~/.longhouse/agent/logs/engine.log.<utc-date>`). `queue_ms` is observation ->
  enqueue; `ship_ms` is enqueue -> accepted. Both in the tens of ms means the
  agent is healthy; seconds means the daemon is starved, and the same log will
  show `Outbox collection was slow`, `Heartbeat POST was slow`, or
  `Local status projection exceeded background budget`.
- **activity receipts** are the provider hook's presence posts as the host
  committed them. `fact_receipts` keeps only the newest handful per subject, so
  an *older* window legitimately shows none; use the local `session_phase_state`
  ledger (printed in the engine section) and the served `activity` fact instead.
- **served activity / presentation.primary** is the badge. It is a 10-minute
  provider-hook observation, never the transcript, so a session that has been
  working for minutes can still read `Idle`/`Blocked`/"Last observed idle".

### The state plane is not the transcript

The rows below the badge come from the transcript and are near-real-time. The
badge comes from the `activity` fact family (`run:<run-id>`, source
`claude_hook`, 10-minute freshness). `needs_user`/`idle` map to `quiescent`;
only `blocked`/`stalled` render as attention. Claude emits no presence event for
`AskUserQuestion` itself, so during a pending question the badge shows whatever
the last hook event said, and for a minute after a turn ends it shows the
`idle_prompt` notification. When the badge contradicts the rows, that is the
expected shape, not a stuck UI: say "state-plane staleness", not "ingest lag".


## Read The Result

- Slow `assistant_tool_to_tool_result` means the tool itself or Claude hook/tool execution is slow.
- Slow `tool_result_to_next_assistant` means provider/model-loop latency.
- A huge gap after `assistant_text` with no following `tool_use`, `Stop`, or `idle` phase is a stuck provider/TUI turn, not tool latency or hosted ingest lag. On a Bedrock Claude flow, also check whether `LONGHOUSE_FORCE_NATIVE_CLAUDE_CHANNELS=1` is using the private native-channel patch.
- Large `cache_read_input_tokens` plus slow `tool_result_to_next_assistant` usually points at provider latency from a large thread, not Longhouse telemetry.
- Hosted `sessions.ended_at` with `session_runtime_state.terminal_state = null` is a state-model mismatch. Treat runtime state as the lifecycle source of truth.
- WriteSerializer waits and high ingest/runtime request counts explain hosted UI/ingest lag, not local provider thinking time, unless a synchronous local hook is slow.

## Session Ran Unregistered

A session that never appears hosted, or has no coordination authority, may have
lost its launch registration rather than its shipping. The evidence is durable:

```bash
ls ~/.longhouse/agent/managed-local/registration-retries/
```

`recovery_exhausted: false` is recovery still running; `true` means it gave up
and that session has no control path for the rest of its life. Read the launch
warnings before blaming the host — they now distinguish *unreachable* (connect
or DNS failed) from *did not answer within Ns* (the host accepted the request
and kept working). Only the first is an outage.

The second means queueing, and queueing here is a defect, not weather. `POST
/api/sessions/managed-local/this-device` goes through catalogd's single writer,
whose product budget is **250ms p95 / 1s alert**
(`control-plane/docs/specs/speed-of-light-database.md`); catalogd allows this
call 10s only to cover cold schema costs after a restart. A registration taking
seconds in steady state is a write-path regression to investigate — do not
"fix" it by widening a client deadline.

Note the trap when measuring: `Heartbeat POST was slow` only logs above 1000ms,
so those lines are the tail, never the distribution. Counting them tells you
how many were slow, not what fraction. And an abandoned request does not stop
the host — a client that walks away leaves the write occupying the single
writer, so aggressive client timeouts *add* queueing rather than shed it.

Successful `Shipped storage-v2 source envelope` lines in the same window are
proof the host was up.

## Hook Check

Claude hooks should be local-only and fast. The installed hook should write local presence/binding state and exit 0. If you suspect hook blocking, measure it with a synthetic event before blaming hosted telemetry.

## Common Sources

- Claude transcript: `~/.claude/projects/**/<session-id>.jsonl`
- Claude channel state: `~/.claude/channels/longhouse/sessions/<session-id>.json`
- Hosted tenant DB: `/var/app-data/longhouse/<subdomain>/longhouse.db` on the runtime host
- Tenant container: `longhouse-<subdomain>`

## Report Shape

End with a verdict:

- `provider latency`
- `tool/hook latency`
- `hosted ingest lag`
- `provider write gap` — the transcript line itself was late; nothing Longhouse-side was slow
- `state-plane staleness` — the transcript is current but the badge is a stale hook fact
- `runtime state mismatch`
- `unknown`

Include the one or two numbers that prove it, not a full transcript dump.
