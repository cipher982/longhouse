# Architecture

A short map of how Longhouse fits together and what its nouns mean. For the
product thesis and invariants, read [`VISION.md`](VISION.md).

## The two components

Longhouse is one product with two public components:

- **Machine Agent** — a Rust engine (`longhouse-engine`) that runs on each
  machine where you do work. It drains the hook output that provider CLIs
  write, ships session events to the Runtime Host with retry/spool, and emits
  heartbeats. This is the shipping path.
- **Runtime Host** — the backend product: a FastAPI API, the bundled web UI,
  and SQLite-backed state. It is what `longhouse serve` runs. It lives where
  durability should live.

On a laptop both run together so you can try it out, but the runtime stops when
the laptop sleeps. For durability you run the Runtime Host on an always-on box
(VPS, homelab, Mac mini) and point your dev machines' Machine Agents at it.

```
  dev laptop ─┐
              ├─ Machine Agent ──ships events──▶ Runtime Host ──▶ web / CLI / iOS
  dev box ────┘                                  (SQLite, durable)
```

## Core principles

- **`/api/agents/*` is the canonical machine surface.** The browser, CLI, MCP,
  and iOS all sit on top of the same primitives — none is a separate source of
  truth.
- **SQLite is the only core database requirement.** Hosted account, billing,
  and provisioning state lives outside this repo.
- **One session, one execution owner.** A session runs somewhere real;
  Longhouse observes or controls it but never silently moves it.
- **Capability over type.** Every item in the timeline is a session. Some have
  live control, some need host reattach, some are search-only. Rely on
  `session.capabilities`, not a session "species".
- **Separate realtime truth from durable archive.** A live lane answers "what
  is happening right now" and must feel terminal-fast; a durable lane answers
  "what provably happened" and must be correct, ordered, and replayable.

## Session modes

Every session has exactly one mode. This vocabulary is canonical here and in
the workspace `AGENTS.md`; nowhere else in this repo should redefine it —
only link to this section.

- **Shadow** — unmanaged, observe-only. The Machine Agent discovered a
  session it did not launch (e.g. a bare `claude` run). Searchable and
  sometimes partially live, but not steerable — Longhouse never owned its
  control path.
- **Helm** — managed, interactive, remote-steerable. A human starts Helm from
  a physical terminal with `longhouse claude`, `longhouse codex`, or another
  supported provider wrapper. Longhouse preserves the provider's normal TUI
  and owns its control path. Once that terminal-originated session exists,
  web/iOS may steer it remotely; web/iOS never originates a headless Helm
  session. The provider process may persist while the interactive TUI remains
  open.
- **Console** — managed, headless, UI-dispatched. Web/iOS sends one-shot work
  through the Machine Agent; the provider process runs one turn and exits,
  state persists to disk, and nothing stays resident between turns.

`Managed session` / `Unmanaged session` (below) remain valid at the
control-ownership level: Unmanaged == Shadow; Managed == Helm or Console.
Both vocabularies are correct — they answer different questions (can
Longhouse steer this at all, vs. exactly how).

### Mapping to code

Read `schemas/managed_providers.yml` for the current, authoritative
per-provider support matrix — it changes independently of this document and
this document does not restate it. The product terms above and the
manifest/engine field names below refer to the same three modes:

| Product term | Manifest/code fields |
| --- | --- |
| Shadow | discovered/unmanaged import; no `launch_*` capability involved |
| Helm | `launch_local`, plus adapter-proven live controls such as `steer_active_turn` and `send_input` |
| Console | `turn_start` (current); `run_once` (legacy, being retired — see `docs/specs/turn-scoped-console-execution.md`) |

`launch_remote` / provider-facing `session.launch` is obsolete compatibility
machinery for persistent no-terminal processes. It is not a fourth mode or a
valid Helm launch surface and is scheduled for deletion by
`docs/specs/turn-scoped-console-execution.md`.

## Glossary

The project uses some shorthand nouns. The important ones:

- **Provider CLI** — an upstream binary you install yourself (`claude`, `codex`,
  Antigravity, `opencode`). Longhouse launches it through a control
  path but does not vendor, pin, or update it.
- **Wall** — a live overview of current sessions across your machines.
- **Recall** — semantic/full-text retrieval over past session history.
- **Tail** — stream the recent events of a session.
- **Peers** — other machines/agents reporting into the same Runtime Host.
- **Runner** — an optional WebSocket command executor for remote execution on
  a user-owned machine.

## Where to read next

- [`VISION.md`](VISION.md) — product thesis and invariants (start here)
- [`docs/README.md`](docs/README.md) — index of design specs
- `docs/specs/agents-machine-surface.md` — the canonical machine contract
- `server/README.md` / `runner/README.md` — component detail
