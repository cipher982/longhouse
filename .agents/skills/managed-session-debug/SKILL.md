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
- `runtime state mismatch`
- `unknown`

Include the one or two numbers that prove it, not a full transcript dump.
