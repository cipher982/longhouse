# Managed Local Session Control

Status: in progress
Owner: David / Longhouse product direction
Updated: 2026-03-21

## Executive Summary

Longhouse should stop treating cloud continuation as the default answer for laptop-originated coding sessions.

The first real product to ship is narrower and better:

- a session is started under Longhouse management on the user's laptop
- the laptop still runs the real Claude Code TUI
- Longhouse knows that session's home is `On this Mac`
- the phone can nudge, reply, or continue that exact session without VNC or terminal typing

This spec intentionally defers all local-to-cloud switching semantics.

The v1 goal is not "seamless takeover." The v1 goal is:

- preserve native laptop Claude Code
- let Longhouse send text back into that same managed local session
- make `/loop` a trustworthy remote steering surface for that session

## Problem

The current Loop/mobile work proved a real UX need, but it also exposed a product flaw:

- a local laptop session and a hosted/cloud continuation are not the same thing
- `Continue` cannot silently change execution venue without breaking trust
- local dirty state, half-finished edits, and terminal-native flow do not transfer cleanly to cloud

The current system is too biased toward:

- completed turn -> follow-up card -> cloud/workspace continuation

That is the wrong default for laptop-first use.

For the common case, the user wants:

- "Claude is already running on my MacBook"
- "I am away from keyboard"
- "My phone should send `continue` or a short reply back into that exact session"

## Product Principles

### 1. Every session has one obvious home

For v1, the only new session home we care about is:

- `On this Mac`

That label must be explicit in both desktop and phone surfaces.

### 2. `Continue` never changes venue

For a managed local session, `Continue` means:

- send the next prompt to the exact managed local Claude session

It does not mean:

- create a continuation session
- move work to hosted/cloud
- branch somewhere else

### 3. Native Claude Code stays native

Longhouse should not replace the Claude Code terminal UI on laptop.

Managed local sessions should run the stock Claude Code TUI under Longhouse-owned launch control.

### 4. Managed local beats arbitrary attach

For v1, Longhouse should support sessions that it launched itself.

Do not try to attach to arbitrary already-open naked Claude terminals as a core product path.

### 5. Loop remains the phone action surface

Telegram is a nudge.

`/loop` is where the user should:

- continue
- reply
- not now

### 6. Reply is first-class

The phone product is not just one-tap approval.

For managed local sessions, users must be able to send a short freeform reply back into the source session.

## Validated Feasibility

The following have already been validated outside the product codepath:

### Managed local tmux control

Running stock Claude Code in `tmux` under controlled launch works, and `tmux send-keys` can inject follow-up prompts into the exact live session.

This preserves:

- the real Claude Code TUI on laptop
- the exact session context
- the exact working tree and filesystem

### Managed headless continuity

Pinned Claude sessions using `--session-id` / `--resume` and `-p` preserve both conversational memory and filesystem/tool continuity across one-off calls.

This remains valuable for hosted-mode work later, but is not the v1 target in this spec.

## Current Codebase Shape

The best existing insertion point is the session chat path:

- `apps/zerg/backend/zerg/routers/session_chat.py`

It already provides:

- per-session locking
- streaming response handling
- transcript/event parsing
- a single API for "send message to a session"

The current bias is just wrong:

- it assumes cloud/headless continuation

The runner transport stack is already present:

- `apps/zerg/backend/zerg/tools/builtin/runner_tools.py`
- `apps/zerg/backend/zerg/services/runner_job_dispatcher.py`
- `apps/zerg/backend/zerg/routers/runners.py`

For v1, that stack is sufficient to transport `tmux` commands to the source machine.

## Decision Log

### Decision: Defer local-to-cloud switching
**Context:** The phone Loop work exposed severe trust issues when `Continue` implicitly meant cloud takeover for laptop-originated work.
**Choice:** Remove local-to-cloud switching from this v1 spec entirely.
**Rationale:** The 80% product is remote steering of the exact local session. Cloud migration can be added later as an explicit action.
**Revisit if:** Managed local proves unworkable or users overwhelmingly start hosted-first sessions.

### Decision: Support only Longhouse-managed local sessions in v1
**Context:** There is no clean Bedrock-compatible official attach API for arbitrary already-open Claude sessions.
**Choice:** V1 only supports local sessions started under Longhouse management.
**Rationale:** This gives Longhouse a reliable tmux/session handle and avoids brittle tty hijacking.
**Revisit if:** A reliable attach primitive emerges, or we later choose to invest in raw PTY attachment.

### Decision: Keep the existing `/sessions/{id}/chat` API as the send-text entrypoint
**Context:** The codebase already has a session chat router, streaming parser, and UI consumer.
**Choice:** Route managed-local messages through the existing session chat path instead of introducing a second chat API first.
**Rationale:** Lowest churn, easiest way to prove the transport split while keeping the surface area small.
**Revisit if:** Local and hosted session semantics diverge enough that one route becomes misleading.

### Decision: Use `tmux` as the initial managed-local transport
**Context:** `tmux send-keys` and pane capture were validated locally and fit the "real Claude TUI on laptop" goal.
**Choice:** V1 managed-local sessions run inside `tmux`, with Longhouse storing tmux metadata and using runner command execution to control them.
**Rationale:** Simple, reversible, preserves native Claude UX, avoids building a custom terminal wrapper first.
**Revisit if:** tmux state proves too brittle, or a dedicated PTY supervisor becomes clearly superior.

### Decision: Reuse shipper-ingested same-session events instead of scraping terminal output
**Context:** Managed local chat needs to preserve the existing `/sessions/{id}/chat` SSE contract without building a brittle terminal parser.
**Choice:** After sending text through tmux, wait for new events on the same session to arrive via the existing shipper/Stop-hook ingest path, then stream those persisted events back to the client.
**Rationale:** Lower complexity, higher fidelity, and it keeps managed-local chat on the same transcript source of truth Longhouse already uses.
**Revisit if:** The shipper path proves too latent or unreliable for same-turn response UX.

## Architecture

### Session homes

This spec introduces a product-level execution home for sessions.

V1 only needs:

- `managed_local`

Later work may add:

- `managed_hosted`
- `cloud_takeover`

This execution-home metadata must live on the session model rather than being inferred from continuation lineage.

### Managed local session metadata

Each managed local session needs enough data to drive remote control:

- execution home: `managed_local`
- source runner id
- source runner name
- local transport: `tmux`
- tmux session name
- source cwd
- source provider session id

### Control path

Phone / desktop action:

- `/sessions/{id}/chat`

Managed local dispatch:

- resolve source runner + tmux metadata
- send a `tmux send-keys` command over the runner transport
- wait for new shipper-ingested events on that same session
- stream those persisted events back over the existing session-chat SSE route

The transcript and session page continue to use existing event/timeline infrastructure.

### Launch path

Longhouse must provide a way to start a managed local Claude session under tmux on a reachable runner.

That launch path should:

- create/update the `AgentSession`
- persist managed-local metadata
- launch stock Claude Code inside `tmux`
- preserve the laptop-native TUI experience

## Implementation Phases

### Phase 0: Spec and task tracking

Create the persistent spec and task doc, grounded in the validated architecture and the current codebase.

**Acceptance criteria**

- spec exists in `docs/specs/`
- task doc exists in `docs/tasks/open/`
- decisions are recorded explicitly

### Phase 1: Managed local session foundation

Introduce the minimum session metadata and backend scaffolding needed to represent a managed local session and render tmux commands deterministically.

Scope:

- add session execution-home metadata
- add managed-local tmux metadata
- add a backend service that builds/validates tmux launch/send/capture commands
- expose execution-home data in session APIs
- add focused unit tests

**Acceptance criteria**

- `AgentSession` can represent `managed_local`
- migration adds the new columns safely
- managed-local command builder exists and is tested
- session APIs expose the new execution-home metadata
- no Loop or session-chat behavior changes yet

### Phase 2: Managed local launcher

Add the backend path to start a managed local Claude session inside tmux on a connected runner.

Scope:

- start managed local session on runner
- persist tmux session metadata
- prove tmux session existence / status

**Acceptance criteria**

- backend can create a managed local session on a reachable runner
- tmux session metadata is persisted
- focused tests cover launch success/failure cases

### Phase 3: Send text into managed local sessions

Route `/sessions/{id}/chat` for `managed_local` sessions through the tmux transport instead of cloud resume.

Scope:

- branch session chat by execution home
- send typed text into managed local session
- wait for same-session events from shipper ingest and stream them back
- keep current hosted/headless path unchanged

**Acceptance criteria**

- `Continue`/chat text for `managed_local` goes to the source tmux session
- same-session identity is preserved
- the existing session-chat SSE contract stays intact
- focused tests prove routing behavior and tmux command dispatch

### Phase 4: Loop phone actions for managed local

Wire Loop actions for managed local sessions:

- `Continue`
- `Reply`
- `Not now`

**Acceptance criteria**

- Loop cards for managed local sessions show `On this Mac`
- `Continue` targets the source session
- `Reply` becomes first-class
- no cloud takeover wording appears in the managed-local path

## Review Notes

This spec intentionally chooses the smallest product that still feels magical:

- managed local only
- real Claude TUI on laptop
- remote steering from phone

If this works well, hosted/headless can become a second explicit start mode later without changing the core trust model.
