---
title: "One Timeline, Five Control Planes"
description: "How Longhouse parses and controls Claude Code, Codex, OpenCode, Antigravity, and Cursor without pretending they are the same product."
status: draft
---

# One Timeline, Five Control Planes

Coding agents are becoming normal development tools, but their sessions are
still trapped inside five very different local products. One writes JSONL,
another has an app server, another keeps its history in SQLite, and another
only offers lifecycle hooks. They differ even more once you want to do more
than read a transcript: send a message from your phone, interrupt a turn, or
resume work after closing a terminal.

Longhouse is my attempt to make that work feel coherent without replacing any
of the providers. It gives Claude Code, Codex, OpenCode, Antigravity, and
Cursor sessions one searchable timeline and a common capability model. The
important qualifier is *capability model*, not a generic agent wrapper. The
providers are not interchangeable, so Longhouse does not pretend they are.

The design rule is simple: preserve the user's upstream CLI and native session
identity, then use the narrowest real control seam that provider exposes.

## The two jobs are different

There are two distinct problems hiding behind “support a coding agent.”

First, Longhouse needs to find and faithfully archive sessions launched outside
of it. That is **Shadow** mode: live observation and search, but no claim that
Longhouse can control a process it did not launch.

Second, it needs an explicit control path for sessions launched through
Longhouse. That is **Helm** when the user keeps working in their terminal, and
**Console** when Longhouse starts a turn from its own UI. A managed session is
not a Longhouse-owned provider binary. It is the user's installed provider CLI
plus a Longhouse-owned control path beside it.

That distinction matters. A readable transcript is not proof that I can steer
the live process. A running process is not proof that it can resume tomorrow.
Every action in Longhouse is exposed per session only when its provider and
current control path can perform it.

## The common layer: archive native evidence, project a useful timeline

The Machine Agent watches the native source on the user's machine, preserves
raw evidence, and projects known records into a common session timeline:
messages, reasoning where available, tool calls, tool results, runtime phase,
and durable session identity. The Runtime Host then makes that archive and its
current capabilities available through the same API, web, CLI, and iOS
surfaces.

The source format is deliberately not abstracted away during ingestion:

| Provider | Native archive source | What makes it interesting |
| --- | --- | --- |
| Claude Code | JSONL in `~/.claude/projects` | Rich event records, tool IDs, compaction and subagent metadata |
| Codex CLI | JSONL in `~/.codex/sessions` | Session metadata establishes canonical identity and fork lineage |
| OpenCode | `opencode.db` SQLite | Sessions, messages, and parts are relational rows rather than a transcript file |
| Antigravity | `brain/<id>/transcript.jsonl` plus a legacy JSON path | Tool results need adjacent planner context to recover call identity |
| Cursor Agent | `store.db` content-addressed SQLite blob DAG | The durable transcript is ordered blobs, not Cursor's lossy JSONL projection |

This is why “we parse agent logs” undersells the problem. Cursor, for example,
needs raw blob preservation and a renderer that understands the known portion
of its graph while retaining unknown data as evidence for later decoders.
OpenCode needs WAL-aware read-only SQLite capture. Claude and Codex need
incremental JSONL parsing with provider-specific identity and parentage rules.
The output is unified; the evidence is not flattened or guessed.

## Claude Code: use its native channel

Claude has the cleanest interactive control surface. `longhouse claude` keeps
the stock Claude terminal UI, installs Longhouse's private local channel, and
binds it to the managed session. Sending a message is channel injection;
active-turn steering is the same path with an explicit steer intent. Interrupt
targets the identity-checked Claude process rather than indiscriminately
killing a process tree.

That makes Claude a strong Helm provider: Longhouse can send, interrupt, steer
an active turn, answer a pause, and later reattach/continue using Claude's
native session identity. A remotely launched Claude session uses the same
channel under a detached terminal wrapper; it is not a separate generic
“headless” agent.

## Codex: control the app server, keep the TUI

Codex gives us a different but equally useful seam. Longhouse starts the stock
`codex` found on the user's `PATH`, starts its app server, and fronts that
server with a local WebSocket relay. The visible terminal is still the stock
Codex TUI attached to that server.

This separates the user interface from the control path without changing who
owns the provider runtime. Longhouse can keep a managed session alive while a
TUI detaches, let a user reattach later, and drive send, interrupt, active-turn
steer, pause answers, and termination through the bridge. For Console work,
Codex also has a separate prompt-and-exit execution adapter; that is a
different lifecycle from a detached interactive TUI.

## OpenCode: turn its local server into the bridge

OpenCode is the most server-shaped integration. Longhouse runs the user's
stock `opencode serve` on loopback, keeps private per-session bridge state,
and lets the user attach the stock OpenCode UI. The control path talks to
OpenCode's own local API: prompt a session, abort an active request, or stop
the recorded server process group.

The bridge is idempotent per Longhouse session, so a retry reconnects to a
healthy server instead of starting a duplicate. It also has a separate
turn-scoped Console path using OpenCode's structured run output.

There is one important limit: asynchronous prompting is not the same as
mid-turn injection. Longhouse supports send, interrupt, terminate, and
reattach for managed OpenCode sessions, but does **not** advertise active-turn
steer or pause answering.

## Antigravity: embrace a narrow hook boundary

Antigravity does not offer the stable remote-control server that Claude,
Codex, or OpenCode do. Its useful native seam is its hook lifecycle.

`longhouse agy` launches the user's `agy` and installs a Longhouse hook/plugin
adapter. The adapter writes phase and transcript-binding evidence, but it also
services a private inbox. A remote message is durably queued; the next
provider-defined safe hook boundary atomically claims it and injects it as a
user message. Longhouse proves that claim before reporting delivery.

This is intentionally modest. Antigravity supports safe-boundary input
injection, not remote launch, reattach, interrupt, active-turn steering,
pause-answering, or Console execution. Calling it “full remote control” would
be worse than leaving the capability out: it would tell users to rely on an
interaction the provider cannot actually guarantee.

## Cursor: two different products, one native source of truth

Cursor is the most unusual integration because its archive and control seams
are both richer than a transcript file.

For Shadow, Longhouse reads Cursor's `store.db` read-only and WAL-aware. The
store is a content-addressed blob graph, so Longhouse preserves the raw rows,
uses Cursor's ordered root snapshot to render messages, reasoning, tools, and
results, and reports typed gaps for material it does not yet understand.

For Helm, Longhouse reserves the native Cursor chat identity, starts the stock
`cursor-agent` TUI in a PTY, and binds it only when hook evidence and the
native store agree. Remote input is accepted only when the exact conversation
is idle. Interrupt is a guarded Ctrl-C against the active generation;
termination is explicit. Cursor can resume a Helm conversation, but it does
not expose a real mid-turn injection API, so queued input is never mislabeled
as steer.

Cursor Console is separate again: each accepted turn runs stock
`cursor-agent --print` against the same native chat identity. Its structured
output is durably recorded before Longhouse projects it live. The process can
exit between turns while the Longhouse thread and Cursor conversation remain
resumable. That is a turn-scoped Console model, not a hidden, permanently
running Cursor process.

## What the capability table really says

| Provider | Managed input | Interrupt | Active-turn steer | Reattach / continue | Console |
| --- | --- | --- | --- | --- | --- |
| Claude Code | Yes | Yes | Yes | Yes | No separate Console adapter |
| Codex CLI | Yes | Yes | Yes | Yes | Yes, one-shot |
| OpenCode | Yes | Yes | No | Reattach only | Yes, turn-scoped |
| Antigravity | Yes, at a safe hook boundary | No | No | No | No |
| Cursor Agent | Yes, when idle | Yes | No | Helm reattach | Yes, turn-scoped |

The table is not a ranking. It is a promise boundary. A checkmark means there
is a provider-native mechanism and a Longhouse control path for the operation;
a blank means we intentionally do not infer one from some weaker signal.

## The abstraction is honesty

The tempting implementation would have been to run every provider in a common
wrapper and call every message a “steer.” It would also be fragile and
misleading. A provider update would break the illusion, or worse, Longhouse
would kill the user's work while trying to repair its own bridge.

Instead, Longhouse keeps one session model, one searchable archive, and one
capability vocabulary while allowing each provider's mechanics to remain
visible. A session can be searchable, live, interruptible, steerable, or
reattachable as separate facts. The browser, CLI, and iOS client consume those
facts rather than guessing from the provider name.

That is the real interoperability layer: not making five coding agents look
identical, but making their differences explicit enough that you can trust the
same timeline for all of them.
