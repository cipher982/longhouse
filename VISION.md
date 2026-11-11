# Longhouse Vision (2026)

Longhouse is mission control for CLI agent sessions that run on machines users own.

The launch product:

- one timeline of every session
- past work that is easy to find and recover
- a real control path kept attached to any session launched through Longhouse

It works on a laptop and shines on a machine that stays on. This is the
north-star document: a proposal that conflicts with it is wrong until this file
changes. Install steps are in [`README.md`](README.md), the system map and
vocabulary in [`ARCHITECTURE.md`](ARCHITECTURE.md), and the truth-plane contract
in [`docs/contracts/truth-plane.md`](docs/contracts/truth-plane.md).

**Naming.** Longhouse is the product and brand. Zerg is the internal repo
codename and stays out of product copy.

## Thesis

1. **Session sync and memory are table stakes.** A user can pull up any Claude,
   Codex, Cursor, OpenCode, Pi, Oh My Pi, or Antigravity session from the web or
   CLI without hunting through local logs.
2. **The wedge is remote control over real sessions on user-owned machines.**
   The product becomes compelling when the user can steer work after launch, not
   just read a transcript.
3. **Transcript plus a reacquirable control path is the strongest form.**
   Imported history is valuable. A thread launched through Longhouse is better:
   Longhouse can control its active turn and resume a later turn on the recorded
   machine. An idle thread does not need an idle provider process.
4. **Hosted is a convenience, not the core truth.** A user who wants durability
   can run on a VPS, Mac mini, homelab box, or any always-on machine they control.
5. **Provider clients stay the execution layer.** Longhouse standardizes
   discovery, history, control and remote access. The installed Claude Code,
   Codex, Cursor or other client keeps its agent loop, authentication, billing,
   tools and semantics. Longhouse does not flatten them into a generic model API
   or make users buy the same model access again.

## Launch story

Install the Longhouse agent on each machine where you run coding sessions. Point
it at an always-on box that runs your Longhouse server (a VPS, homelab, or Mac
mini). Longhouse imports sessions started in provider terminals, new ones by
default and older history only when the machine's owner chooses. From the web or
iOS, a user can pick a provider and a connected machine and launch Console work
there. From a terminal, `longhouse <provider>` launches Helm with the provider's
normal TUI. The provider client does the work with its own account and local
environment, and Longhouse keeps the session reachable from every client.

The product must make these plain:

- the Machine Agent runs where work happens
- the Runtime Host runs where durability should live (an always-on machine, or hosted)
- a laptop can run both for a trial, and stops when it sleeps; that is not a failure
- durability comes from where you put the server, not from migration

## Acquisition

One product with two components and several channels:

- macOS users learn `Longhouse.app`
- agents, non-GUI users and power users keep the CLI paths
- hosted, where we run the server, comes later

Every channel ends in the same topology: one Machine Agent per dev machine, one
Runtime Host where durability lives, one machine-facing control path, and one
health and repair surface. Nobody should need to understand shell bootstrap,
launchd, or helper binaries to answer "is Longhouse installed and healthy here?"

## Invariants

1. **One session, one execution owner.** Longhouse may observe, control or
   branch from a session. It never silently moves one.
2. **Capability over type.** Every timeline item is a session. Some have live
   control, some need host reattach, some are search-only. No separate species
   of session in the product story.
3. **Machine contract first.** CLI, HTTP, the browser and native wrappers sit on
   the same primitives. The browser and native app are bundled views, not the boundary.
4. **MCP is an adapter.** Useful capabilities must make sense without it.
   Browser, native, MCP and future assistants are clients of the same session
   model, not middlemen.
5. **SQLite is the only core database.** Hosted account, billing and
   provisioning state lives outside this public core.
6. **Self-host first, hosted later.** The product is understandable and useful
   before hosted provisioning exists.
7. **Behavior is explicit.** No hidden fallbacks, silent mode switches, or
   capability logic duplicated across frontend, backend and clients. One source
   of truth per capability; clients do not infer different meanings from the same raw fields.
8. **Separate realtime truth from durable archive.** The live lane answers
   "what is happening right now". First visible output should reach the browser
   over WebSocket or SSE in tens to hundreds of milliseconds under nominal
   network. The durable lane answers "what provably happened": correct, ordered,
   replayable and retryable, trailing the live lane by a small bounded window.
   Never weaken archive correctness to chase the live-lane target.
9. **Persist threads, not idle provider processes.** A Console invocation is
   scoped to one nonterminal turn. Tool waits keep the turn active, and
   `end_turn` starts a bounded output drain and releases the invocation. A later
   message resumes the durable provider thread in a new invocation. Helm may
   keep a process while the user's TUI is open, but process lifetime is never
   session identity.
10. **Prefer seams and deletion.** Different behaviors are different contracts.
    A smaller honest product beats a broader confusing one. Design for cold
    restarts: durable artifacts beat in-memory state.

## Scope

**What Longhouse is:**

- one searchable timeline, with raw session detail, search and recall
- remote control for sessions launched through Longhouse, over the installed provider clients and their existing accounts
- coordination primitives: wall, peers, tail, message, continue
- runner-backed execution on user-owned machines
- a bundled web UI over the machine contract, and a quiet macOS app that stays ambient and opens a clear status or repair path on demand

**What it is not:** an AI dashboard, a replacement agent runtime or model proxy,
a reseller of provider subscriptions, a tool that silently moves running
sessions between machines, a generic remote shell manager, a mailbox or jobs
platform, or a product that needs an execution-home taxonomy in its user story.

**Launch core:**

- session ingest for Claude Code, Codex CLI, Cursor Agent, OpenCode, Pi Agent, Oh My Pi and Antigravity CLI
- timeline, search, session detail and recall
- the `/api/agents/*` machine surface with CLI parity
- managed-local launch and remote control on user-owned machines
- three capability states per session: live control, host reattach, search-only

**Launch support:** demo data and local onboarding that reach value fast; runner-backed execution.

**Frozen or removed for launch:** implicit takeover of a running local session
(explicit branch-from-session can come later as a new session); loop inbox and
turn reviews; email surfaces (hidden from nav); jobs as a user-facing surface;
briefings and insights pages; proactive operator mode; broad hosted self-serve.

**Later, only if it strengthens the same loop:** hosted as another explicit
launch target, stronger provider parity, email or conversation surfaces, and
workflow layers on the session kernel. None is part of the launch promise.

## Priorities and decision filter

Prelaunch order: make ingest, search and memory boring and reliable; make
managed-local remote control boring and reliable; remove ambiguous or
half-supported surfaces before polishing; keep the hosted story narrow and
explicit; ship one honest product loop.

Before adding or keeping a feature, ask whether it improves finding past work,
improves steering live work on user-owned machines, or simplifies the product
story. If none, freeze or cut it.
