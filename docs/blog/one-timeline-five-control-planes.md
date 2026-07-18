---
title: "Longhouse Provider Integrations"
description: "Parsing and managed-control paths for Claude Code, Codex, OpenCode, Antigravity, and Cursor."
status: draft
---

# Longhouse Provider Integrations

Longhouse provides one session archive and capability model for Claude Code,
Codex, OpenCode, Antigravity, and Cursor. It does not replace provider CLIs,
their terminal UIs, or their native session identities.

Each provider has a different archive format and a different control surface.
Longhouse keeps those differences explicit. A capability is shown only when the
provider and the current session control path support it.

## Operating model

**Shadow** sessions are discovered from native files or databases. They are
searchable and observable, including live archive updates, but Longhouse does
not control the provider process.

**Helm** sessions are launched through Longhouse and retain the provider's
normal interactive terminal UI. Longhouse owns a separate control path for the
session.

**Console** sessions are launched from Longhouse UI. A provider invocation is
scoped to a turn; the durable thread remains after the provider process exits.

Managed control does not imply that Longhouse owns the provider binary. Each
path uses the user-installed upstream CLI.

## Archive sources

The Machine Agent captures provider-native source data, retains raw evidence,
and projects known records into a common timeline. The normalized timeline
contains messages, tool calls, tool results, runtime state, and durable session
identity. Unknown source material remains raw evidence instead of being
guessed or discarded.

| Provider | Native archive source | Parsing details |
| --- | --- | --- |
| Claude Code | JSONL in `~/.claude/projects` | Incremental records preserve tool IDs, compaction boundaries, subagent metadata, and working-directory context. |
| Codex CLI | JSONL in `~/.codex/sessions` | Session metadata provides canonical identity and fork lineage. |
| OpenCode | `opencode.db` SQLite | Session, message, and part rows are captured read-only, including WAL-driven updates. |
| Antigravity | `brain/<id>/transcript.jsonl` and a legacy JSON path | Planner context is used to associate tool results with calls. |
| Cursor Agent | `store.db` SQLite blob DAG | Ordered source blobs are retained and rendered; unknown blobs remain typed render gaps. |

The archive format is provider-specific. The session projection is shared.

## Claude Code

`longhouse claude` runs the stock Claude terminal UI with Longhouse's private
local channel. The channel binds to the managed session and provides input
injection. A process-identity check limits interrupts to the matching Claude
process.

Supported managed operations:

- send input;
- interrupt;
- active-turn steer;
- answer a pause;
- reattach or continue using the native session identity.

Remote launch uses the same channel under a detached terminal wrapper. It is
not a separate one-shot Console adapter.

## Codex

`longhouse codex` resolves the stock `codex` binary from `PATH`. Longhouse
starts Codex app-server, places a local WebSocket relay in front of it, and
attaches the stock Codex TUI to that server.

The TUI and the control path are independent. A detached TUI does not imply
that the managed session has ended. Longhouse retains the bridge until an
explicit stop path terminates it.

Supported managed operations:

- send input;
- interrupt;
- active-turn steer;
- answer a pause;
- reattach or continue;
- run a separate one-shot Console invocation.

Longhouse does not distribute or patch Codex. The managed path always uses the
user-installed upstream binary unless an explicit operator override is set.

## OpenCode

`longhouse opencode` runs stock `opencode serve` on loopback and attaches the
normal OpenCode UI. Bridge state retains the local server address, provider
session identity, process identity, and credentials needed to reconnect.

Input maps to OpenCode's prompt API. Interrupt maps to its abort API. The
managed server is idempotent per Longhouse session, so a retry reconnects to a
healthy bridge instead of creating a second server.

Supported managed operations:

- send input;
- interrupt;
- terminate;
- reattach;
- run a turn-scoped Console invocation.

OpenCode does not expose a proven mid-turn injection mechanism. Longhouse does
not advertise active-turn steer or pause-answer for OpenCode.

## Antigravity

`longhouse agy` runs the user's `agy` CLI and installs a hook/plugin adapter.
The adapter records phase and transcript-binding information and exposes a
private input inbox.

A remote message is queued in the inbox. A provider hook claims it at the next
safe boundary and returns it as a user message. Longhouse waits for that claim
before reporting delivery.

Supported managed operation:

- queued input injection at a provider-defined safe hook boundary.

Unsupported operations:

- remote launch;
- reattach or later continuation;
- interrupt or terminate;
- active-turn steer;
- pause-answer;
- Console execution.

The hook boundary is not a general remote-control server. The capability is
exposed as safe-boundary input injection, not as live steer.

## Cursor

Cursor uses two separate managed paths.

### Helm

`longhouse cursor` reserves a native Cursor chat identity and runs the stock
`cursor-agent` TUI in a PTY. Hook evidence and the native `store.db` source
must agree before the managed session is bound. The control path uses a
per-session Unix socket.

Input is accepted only when the exact Cursor conversation is idle. Interrupt
uses Ctrl-C only for a verified active generation. Termination is explicit.
Cursor supports Helm reattach, but it does not provide a real mid-turn input
API.

Supported Helm operations:

- send input while idle;
- interrupt;
- terminate;
- reattach.

### Console

Cursor Console runs one stock `cursor-agent --print` invocation per turn. Each
turn uses the same native chat identity. Structured output is written to
durable files before it is projected into the Longhouse timeline. The provider
process can exit after a turn while the Longhouse thread and Cursor chat remain
available for a later turn.

Cursor Console does not advertise active-turn steer or generic pause-answer.

## Capability matrix

| Provider | Managed input | Interrupt | Active-turn steer | Reattach / continue | Console |
| --- | --- | --- | --- | --- | --- |
| Claude Code | Yes | Yes | Yes | Yes | No separate Console adapter |
| Codex CLI | Yes | Yes | Yes | Yes | Yes, one-shot |
| OpenCode | Yes | Yes | No | Reattach only | Yes, turn-scoped |
| Antigravity | Safe hook boundary only | No | No | No | No |
| Cursor Agent | Yes, when idle | Yes | No | Helm reattach | Yes, turn-scoped |

The matrix is an operation-level contract. Archive visibility, runtime state,
process liveness, managed ownership, and control availability are represented
separately. A provider name alone does not determine whether a session can be
controlled.

## Design constraints

- Provider CLIs remain user-owned.
- A provider's archive format remains the durable source of evidence.
- Managed control uses an explicit provider-native channel, bridge, API, hook,
  or terminal contract.
- A missing control path degrades capability; it does not terminate provider
  execution.
- Unsupported operations remain unavailable instead of being approximated with
  terminal automation or inferred state.

Longhouse exposes one timeline and one session model while retaining the
provider-specific behavior required for accurate parsing and control.
