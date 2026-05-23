# Longhouse Vision (2026)

Longhouse is mission control for CLI agent sessions that run on machines users own.

The launch product is simple:

- bring every session into one timeline
- make past work easy to find and recover
- keep a real control path attached when a session is launched through Longhouse

Works on your laptop. Shines on a machine that stays on.

This is the north-star document. It is intentionally short. If a proposal conflicts with this file, the proposal is wrong until this file changes.

## Read Next

- `README.md` for the install and demo loop
- `docs/specs/agents-machine-surface.md` for the canonical machine-facing contract
- `docs/specs/macos-launch-product-shape.md` for the macOS launch-product decision
- `docs/specs/prelaunch-simplification-cut-plan.md` for the launch-pruning order

## Naming

- **Longhouse** = product and brand
- **Zerg** = repo/internal codename

Use Longhouse in product copy. Keep Zerg internal.

## Product Thesis

1. **Session sync and memory are table stakes.**
   Users should be able to pull up any relevant Claude, Codex, Antigravity, or OpenCode session from the web or CLI without hunting through local logs.

2. **The wedge is remote control over real sessions running on user-owned machines.**
   The product becomes compelling when the user can steer work after launch, not just read a transcript.

3. **Longhouse is strongest when it has both the transcript and a control path.**
   Imported history is valuable. A session launched through Longhouse is better because it stays addressable later.

4. **Hosted is a convenience path, not the core truth.**
   If a user wants durability, they should be able to run on a VPS, Mac mini, homelab box, or other always-on machine they control.

## Launch Story

The launch story should fit in one paragraph:

Install the Longhouse agent on each machine where you run coding sessions. Point it at an always-on box where your Longhouse server lives — a VPS, homelab, or Mac mini. Import existing sessions or start new ones through Longhouse. Find old work fast, inspect the raw session, and steer live work later from the web UI, CLI, or HTTP.

That means the product should make these truths obvious:

- the Machine Agent runs where work happens (your laptop, your dev box)
- the Runtime Host runs where durability should live (an always-on machine or hosted)
- a laptop can run both for trying it out, but it stops when the laptop sleeps — that is not a system failure
- durability comes from where you put the server, not magic migration

## Acquisition Model

Longhouse launches as one product with two components and multiple acquisition channels:

- macOS humans should learn `Longhouse.app`
- agents, headless users, and power users should keep the CLI paths
- hosted is a later convenience path — we run the server for you

All channels converge on the same topology:

- one Machine Agent per dev machine, shipping sessions
- one Runtime Host where durability lives
- one machine-facing control path
- one health and repair surface

The product should never make users understand shell bootstrap, launchd, or helper binaries just to answer "is Longhouse installed and healthy here?"

## Product Invariants

1. **One session, one execution owner.**
   A session runs somewhere real. Longhouse may observe it, control it, or branch from it, but it does not silently move it.

2. **Capability over type.**
   Every item in the timeline is a session. Some have live control, some need host reattach, some are search-only. Do not invent separate species of session in the product story.

3. **Machine contract first.**
   The canonical boundary is the machine-facing contract. CLI, HTTP, the browser, and native desktop wrappers all sit on top of the same primitives.

4. **Human surfaces are bundled views, not the boundary.**
   The browser and native local app are part of the product, but neither should become a separate source of truth.

5. **MCP is an adapter, not the platform boundary.**
   Useful capabilities must make sense without requiring MCP.

6. **Assistant surfaces are clients, not middlemen.**
   Browser, native, MCP, and future assistants consume the same session model.

7. **SQLite is the only core database requirement.**
   Hosted account, billing, and provisioning state belongs outside this public core.

8. **Self-host first, hosted later.**
   The product must be understandable and useful before hosted provisioning exists.

9. **Keep behavior explicit.**
   No hidden fallbacks, silent mode switches, or duplicated capability logic across frontend, backend, and clients.

10. **Separate realtime truth from durable archive.**
    Managed sessions have two lanes. The live lane answers "what is happening right now" and must feel terminal-class: first visible output should arrive in the browser over WebSocket/SSE in tens to hundreds of milliseconds under nominal network. The durable lane answers "what provably happened" and must be correct, ordered, replayable, and retryable; it can trail the live lane by a small bounded window. Do not weaken archive correctness to chase the live-lane SLA.

## What Longhouse Is

- one searchable timeline for agent sessions
- raw session detail, search, and recall
- remote control for sessions launched through Longhouse
- coordination primitives such as wall, peers, tail, message, and continue
- runner-backed execution on user-owned machines
- a bundled human UI over the same machine contract
- a quiet native macOS app that can stay ambient and still open a clear status or repair path on demand

## What Longhouse Is Not

Longhouse is not:

- an AI dashboard
- magical local-to-cloud takeover
- a generic remote shell manager
- a mailbox product before launch
- a jobs platform before launch
- a product that needs a complicated execution-home taxonomy in the user-facing story

## Launch Surface

### Core

- session ingest for Claude Code, Codex CLI, Antigravity CLI, and OpenCode (legacy Gemini imports stay searchable)
- timeline, search, session detail, and recall
- canonical `/api/agents/*` machine surface plus CLI parity
- managed-local launch and remote control on user-owned machines
- clear capability states for sessions:
  - live control
  - host reattach
  - search-only

### Support

- demo data and local onboarding that get users to value fast
- runner-backed execution on user-owned machines

### Frozen or removed for launch

- cloud-branch / cloud-takeover (capability gate is off, code remains)
- loop inbox and turn reviews (removed); email surfaces (hidden from nav)
- jobs as a user-facing product surface
- briefings and insights as standalone pages
- proactive operator mode
- broad hosted self-serve

## Architecture Constraints

- **No Postgres in core/runtime.** SQLite only.
- **Keep the machine surface canonical.** Do not make browser-only routes or MCP wrappers the source of truth.
- **Prefer obvious seams over overloaded ones.** Different behaviors should be different contracts.
- **Prefer deletion over half-supported surfaces.** A smaller honest product is better than a broader confusing one.
- **Design for cold restarts.** Durable artifacts beat clever in-memory state.
- **Keep one source of truth per capability.** Frontend, backend, and clients should not each infer different meanings from the same raw fields.

## Prelaunch Priorities

1. Make session ingest, search, and memory boring and reliable.
2. Make managed-local remote control boring and reliable.
3. Remove ambiguous or half-supported surfaces before adding polish.
4. Keep the hosted story narrow and explicit.
5. Ship one honest product loop, not three overlapping ones.

## Later, If There Is Pull

Later features are allowed only if they strengthen the same core loop:

- hosted as another explicit launch target
- stronger provider parity
- email, inbox, or conversation surfaces
- higher-level workflow layers on top of the session kernel

None of those should be part of the core launch promise.

## Decision Filter

Before adding or keeping a feature, ask:

1. Does it improve finding past work?
2. Does it improve steering live work on user-owned machines?
3. Does it simplify the product story?

If the answer is no, freeze it or cut it.
