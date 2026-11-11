---
name: managed-session-debug
description: Diagnose a managed Longhouse session (Claude, Codex, OpenCode, Cursor, Pi, OMP, Antigravity) that feels slow, looks stuck, shows the wrong badge or lifecycle state, never registered, or disagrees between the local CLI and the hosted timeline. Separates provider-loop latency, hook/tool latency, ship and ingest lag, and state-plane staleness, and ends with a verdict.
---

# Managed Session Debug

For provider control paths and mode definitions see `managed-provider-cli`; for
hosted tenant and database work see `zerg-hosted-debug`.

## Fast path

For "is the transcript shipping live?", read `~/.longhouse/agent/engine-status.json`
(`ship_lanes.live`) and the `Shipped transcript` lines in the engine log before reading code.

| Step | Command |
|---|---|
| Local truth | `longhouse local-health --json \| jq '.managed_sessions[]? \| select(.session_id=="<id>")'` |
| Provider transcript timing | `scripts/ops/session-transcript-timing.mjs <id>` |
| Hosted truth | `scripts/ops/hosted-session-debug.sh --subdomain <tenant> --session <id> --limit 20 --json` |
| Every hop, post hoc | `python scripts/ops/session-realtime-trace.py --subdomain <tenant> --session <id> [--since <iso> --until <iso>] [--json]` |
| Claude process and channel | `ps -axo pid,ppid,lstart,command \| rg '<id>\|claude-channel\|longhouse-channel'` and `jq . ~/.claude/channels/longhouse/sessions/<id>.json` |

`session-realtime-trace.py` prints provider events, engine ships, hosted chunk
commits, activity receipts and the served payload, then names the slow hop. Run it
on the machine that owns the session (the first two sections are local); the rest
comes from the Runtime Host over ssh (`--ssh`, default `zerg`). It is read-only and
needs only the device token at `~/.longhouse/machine/device-token`. The tenant for
David's sessions is `david010`, the hosted helper's default.

## Reading the result

| Signal | Meaning |
|---|---|
| Slow `assistant_tool_to_tool_result` | The tool or the Claude hook/tool execution is slow |
| Slow `tool_result_to_next_assistant` | Provider or model-loop latency. Large `cache_read_input_tokens` alongside it points at a big thread, not Longhouse |
| Long gap after `assistant_text` with no `tool_use`, `Stop` or `idle` phase | Stuck provider/TUI turn, not tool latency or ingest lag. On Bedrock Claude also check the channel patch (`private-claude-bedrock-channels`) |
| provider to hosted-commit lag (`render_objects.created_at` minus the chunk's last event) | Steady state is 1-7 s. One chunk far above that is a write gap: the provider held the line (Claude buffers while a dialog is open) or the fsevent was late. Check whether other paths shipped normally in the same seconds |
| Engine `queue_ms` / `ship_ms` (path-job log `~/.longhouse/agent/logs/engine.log.<utc-date>`) | Tens of ms is healthy. Seconds means a starved daemon; look for `Outbox collection was slow`, `Heartbeat POST was slow`, `Local status projection exceeded background budget` |
| WriteSerializer waits, high ingest/runtime request counts | Hosted UI or ingest lag, not local provider time (unless a synchronous local hook is slow) |
| Legacy `sessions.ended_at` set, `terminal_state` null | A mismatch to explain, not a verdict. Served state and command authority come from catalogd fact snapshots (`live_sessions`, `live_runtime_state`); `session_runtime_state` is legacy evidence |

`fact_receipts` keeps only the newest few per subject, so an older window shows
none; use the local `session_phase_state` ledger (engine section) and the served
`activity` fact instead.

## The badge is not the transcript

Timeline rows come from the transcript and are near real time. The badge
(`presentation.primary`) comes from a separate axis: provider-hook observations
with a 10-minute window. A session working for minutes can still read
`Idle`, or "Last observed idle".

- A wait is only ever a keyed interaction. `needs_answer` and `needs_approval`
  require a pending interaction row; a provider saying it is blocked is not enough,
  and `blocked` is not a presentation state.
- Claude's waits are keyed by its hooks: `PreToolUse` on the pause tool opens a
  question keyed by `tool_use_id`; `PermissionRequest` opens an approval (keyed by
  tool name plus a digest of the input); `PostToolUse` and `PostToolUseFailure`
  retire both. They ride the durable runtime-events lane. A lost hook is retired
  when the provider continues or its run ends.
- A hook provider publishes on transitions, so its last phase stays current for the
  contract window. A quiet badge does not mean the session stopped.

When the badge contradicts the rows, say which axis you checked and what the
interaction row says.

To check a live question end to end, run the trace while the dialog is up and read
`## served`: `pending_interaction` non-null with `kind: question` or `permission`
and `presentation.primary.key` in `needs_answer`/`needs_approval`. After the answer,
both are null and `blocked` appears nowhere. The trace also shows the hook's opening
receipt. A real Claude Helm session cannot be driven from macOS (Keychain prompts);
use a Linux canary or a session that already has the hook.

## Session ran unregistered

A session that never appears hosted, or has no coordination authority, may have lost
its launch registration rather than its shipping. Evidence:
`ls ~/.longhouse/agent/managed-local/registration-retries/`. `recovery_exhausted:
false` means recovery is still running; `true` means it gave up and the session has
no control path for life.

Launch warnings separate "unreachable" (connect or DNS failed) from "did not answer
within Ns". Only the first is an outage. The second is queueing, which is a defect:
`POST /api/sessions/managed-local/this-device` goes through catalogd's single writer
with a budget of 250 ms p95 and a 1 s alert (`control-plane/docs/specs/speed-of-light-database.md`;
catalogd allows 10 s only for cold schema costs). Seconds in steady state is a
write-path regression; do not fix it by widening a client deadline, and note that an
abandoned request still occupies the writer.

`Heartbeat POST was slow` logs only above 1000 ms, so those lines show the tail, not
the distribution. Successful `Shipped storage-v2 source envelope` lines in the same
window prove the host was up.

## Hook check

Claude hooks must be local-only and fast: write presence/binding state and exit 0.
If you suspect one is blocking, time a synthetic event before blaming hosted telemetry.

## Sources

- Claude transcript: `~/.claude/projects/**/<id>.jsonl`; channel state: `~/.claude/channels/longhouse/sessions/<id>.json`
- Hosted live catalog: `/var/app-data/longhouse/<tenant>/longhouse-live.db` on cube; container `longhouse-<tenant>`

## Verdict

End with one of: `provider latency`, `tool/hook latency`, `hosted ingest lag`,
`provider write gap` (the transcript line itself was late), `state-plane staleness`
(transcript current, badge is a stale hook fact), `runtime state mismatch`,
`unknown`. Give the one or two numbers that prove it, not a transcript dump.
