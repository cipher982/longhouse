# Managed-Local Native Codex Demo

Status: In progress
Task: `docs/tasks/open/managed-local-native-codex.md`
Updated: 2026-03-27

## Executive Summary

The current tmux-backed managed-local Codex path proved the product need, but it also proved the wrong abstraction:

- tmux can keep a session alive
- tmux can inject text
- tmux cannot be made interactively invisible enough for a launch-quality Codex demo

The demo-worthy path is to stop wrapping the human UI in tmux and instead split the system into:

- a **native Codex TUI** that the user interacts with directly
- a **local Longhouse bridge** that observes and controls the same Codex app-server session out-of-band

For the demo slice, the bridge should live in Rust inside `longhouse-engine`, launch a local `codex app-server` websocket listener, and let the stock TUI connect through `codex --remote`.

This gives us the right product story:

- `longhouse codex` feels like normal Codex
- Longhouse syncs the session live
- Loop/away-mode can steer the exact same laptop thread later
- tmux remains only as a guarded fallback path during rollout

## Demo Target

The demo is successful when the following flow works on one real laptop and one remote surface (phone or browser):

1. User runs `longhouse codex` in a repo.
2. Stock Codex TUI opens locally with normal scrolling/input behavior.
3. Within a couple of seconds, the session appears live in Longhouse.
4. Assistant progress and transcript updates stream into Longhouse while the local TUI is running.
5. The user leaves the laptop and sends `continue` or a short reply from Loop.
6. Longhouse routes that command back into the same local Codex thread.
7. The user returns to the laptop and sees the same thread, not a cloud clone or a second session.

## Product Principles

### 1. Native UI Is Non-Negotiable

The human interactive path must be stock Codex, not Codex-inside-tmux and not a Longhouse-owned faux terminal.

If the UX differs from native Codex, it should only differ because Codex itself differs, not because Longhouse inserted a terminal layer.

### 2. Longhouse Must Be Out-of-Band

Longhouse should observe and control Codex from the side:

- app-server notifications for live sync
- hooks for belt-and-suspenders lifecycle truth
- explicit structured control messages for away-mode

Not by:

- scraping the screen
- faking keystrokes
- owning the PTY the user interacts with

### 3. The Local Machine Remains The Session Home

This demo is about managed-local continuity, not cloud takeover.

`Continue` must mean:

- keep using the same local thread

It must not mean:

- silently migrate work to hosted Longhouse
- create a second hidden continuation session

### 4. The Bridge Owns Session Lifetime

The TUI is a client.

The bridge owns:

- the app-server process
- the Longhouse correlation metadata
- the local durable journal
- the backend command channel

That way the TUI can disconnect/reconnect without destroying the managed session.

## Why This Architecture

### Codex already has the right primitives

Local validation and upstream source inspection show the pieces we need already exist:

- `codex --remote ws://...` connects the stock TUI to a websocket-backed app-server.
- `codex app-server --listen ws://IP:PORT` exposes Codex over websocket.
- app-server auto-attaches thread listeners on `thread/start` and `thread/resume`.
- app-server tracks subscribers per thread, not a single exclusive client.

That means Longhouse does not need to fake a TUI. It can become another client of the same app-server-backed session.

### The current runner is the wrong first bridge

The existing runner transport is one-shot command execution:

- single active job per runner
- stdout/stderr chunk relay
- no long-lived stdin channel
- no durable process session handle
- no native request/response stream for approvals or app-server notifications

Turning it into a full-duplex persistent Codex transport is possible, but it is larger than the demo slice and solves the wrong problem first.

For the demo, the right move is a purpose-built local bridge in Rust.

## Proposed Architecture

### 1. `longhouse-engine codex-bridge`

Add a new long-lived Rust command inside `longhouse-engine`.

Responsibilities:

- start and supervise `codex app-server`
- prefer loopback websocket transport (`ws://127.0.0.1:<port>`)
- force-enable `codex_hooks`
- stamp `--session-source longhouse_managed`
- connect to app-server as Longhouse's control/observer client
- maintain the Longhouse session ID ↔ Codex thread ID mapping
- receive `thread/*`, `turn/*`, `item/*`, `hook/*`, and approval requests
- persist a local journal for replay/recovery
- sync live events and state to the Longhouse backend
- receive remote commands from Longhouse and translate them into app-server calls

### 2. Stock Codex TUI via `--remote`

The user-facing process remains stock Codex:

- `codex --remote ws://127.0.0.1:<port>`

Longhouse should not proxy or render the TUI itself.

The wrapper may still set small launch-time defaults when justified, but only if they do not visibly degrade the native experience.

### 3. Backend bridge channel

The backend needs a dedicated control path for managed-local native Codex sessions.

Minimum responsibilities:

- register the bridge connection against a Longhouse session
- ingest live app-server-derived events
- persist minimal transport metadata
- queue and deliver remote commands (`continue`, `reply`, `interrupt`, approvals)
- expose current state to session detail / forum / Loop

For the demo, this should be a dedicated authenticated websocket or similarly low-latency persistent channel between the bridge and the tenant backend.

### 4. Hooks stay enabled

Hooks are still valuable:

- presence truth
- transcript shipping fallback
- reconciliation if the live bridge channel misses anything

But hooks should no longer be the primary live-sync mechanism for the managed-native path. The primary live truth is the app-server event stream.

## Control And Data Flow

### Launch

1. User runs `longhouse codex`.
2. Longhouse creates or reserves a managed-local session record in the backend.
3. The local CLI starts `longhouse-engine codex-bridge` with the session ID, cwd, and auth context.
4. The bridge starts `codex app-server` on loopback websocket and connects as Longhouse's control client.
5. The wrapper launches `codex --remote` against that local websocket.
6. The bridge correlates the active Codex thread with the Longhouse session and reports readiness.

### Live sync

1. Codex emits app-server notifications as the user works.
2. The bridge converts them into Longhouse-friendly event/state updates.
3. The backend updates presence, live transcript state, and any timeline/forum surfaces.
4. Hooks continue to send lifecycle/transcript signals as backup truth.

### Away-mode control

1. The user hits `continue` or sends a reply from Loop.
2. The backend sends a command to the bridge for that exact Longhouse session.
3. The bridge turns it into `turn/start`, `turn/steer`, `turn/interrupt`, or approval responses.
4. New Codex output streams back through the same live sync path.

## Launch-Ownership Decision

There is one remaining launch-sequence question:

- should the bridge create/resume the thread first and let the TUI attach to it?
- or should the TUI create the thread and let the bridge adopt it immediately?

This is a validation item, not an architectural blocker.

The recommended bias is:

- let the bridge own the session if remote TUI attach UX is clean
- otherwise let the TUI create the first thread and have the bridge adopt/correlate it

Either way, the bridge must end up owning the durable Longhouse mapping and away-mode control path.

## Non-Goals For This Demo Slice

This spec does not attempt to solve:

- attaching Longhouse to arbitrary already-running naked `codex` sessions
- cross-machine takeover of a managed-local thread
- runner protocol redesign for generic persistent interactive subprocesses
- a provider-generic native path for Claude/Gemini at the same time
- replacing the existing tmux fallback before parity is proven

## Milestones

### M1. Dual-client validation

Prove the local topology before product integration:

- stock Codex TUI via `--remote`
- Longhouse bridge as a second websocket client
- shared visibility into the same thread
- hooks still firing under the same managed session
- clarity on whether the bridge or TUI should own initial thread creation

Exit criteria:

- one real local run proves TUI + bridge coexist on the same managed session without UX degradation

### M2. Rust bridge MVP

Build `longhouse-engine codex-bridge` with:

- app-server supervision
- websocket client connection
- local journal
- session/thread correlation
- approval request handling
- structured logging

Exit criteria:

- the bridge can run standalone and maintain a managed Codex session locally

### M3. Live sync into Longhouse

Add backend support for:

- bridge registration/auth
- live event ingest
- current-state storage
- surfacing native-managed sessions in Longhouse UI

Exit criteria:

- a native-managed Codex session appears live in Longhouse while the user is still in the local TUI

### M4. Loop and away-mode control

Route remote commands through the bridge:

- continue
- short freeform reply
- interrupt
- approval response

Exit criteria:

- a remote Loop action changes the exact local Codex thread and the result appears both remotely and locally

### M5. Wrapper and demo polish

Finish the user-facing launch path:

- `longhouse codex` experimental native path
- guarded fallback to tmux
- readiness / reconnect behavior
- demo script and QA checklist

Exit criteria:

- David can reliably demo the flow without explaining transport caveats

## Acceptance Criteria

The demo slice is done when all of the following are true:

- `longhouse codex` opens a stock Codex TUI without tmux in the interactive path.
- The local session syncs live into Longhouse without waiting for the stop hook.
- Longhouse can correlate the managed Longhouse session to the real Codex thread durably.
- Loop can send `continue` and short freeform replies into that same local thread.
- Interrupts and approval requests are handled structurally, not by fake typing.
- Returning to the laptop shows the same Codex thread progressing, not a copied cloud continuation.
- tmux remains available as an explicit fallback during rollout.

## Biggest Remaining Risks

### 1. Remote TUI attach semantics

We still need a crisp validation of the best launch order for `codex --remote` versus bridge-owned thread creation.

### 2. Real approval-flow proof

The canary now handles approval requests structurally, but we still need a deterministic real-binary proof path that emits them on demand.

### 3. Bridge reconnect behavior

Laptop sleep, backend reconnects, and temporary network loss need recovery rules so the bridge does not orphan a session.

### 4. Event-model mapping

We need a clean mapping from app-server notifications into:

- live transcript updates
- presence state
- durable session detail rendering
- final transcript reconciliation

## Recommendation

Build the demo on the local Rust bridge path now.

Do not spend the next phase extending tmux or redesigning the generic runner protocol first.

That work may still matter later, but it is not the shortest path to a demo where `longhouse codex` feels native, syncs live, and can be steered remotely on the same thread.
