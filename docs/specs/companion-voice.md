# Companion — native-adapter concierge thread with voice entry

Status: draft v2 (2026-07-19), rewritten after codebase research; supersedes
the v1 "designation over Helm pty" draft entirely.

## Context

David wants a zero-friction, on-the-go way to talk to an agent that has his
full context (global AGENTS.md, skills, life hub, sauron, email, repos): tap
a button on iOS, speak, hear an answer in ~2s, walk away. Both "ephemeral
Siri-style question" and "long-running single thread" collapse into one
durable thread with zero per-interaction setup. The host must be always-on
(laptop is closed in a backpack); cinder must be fungible computation — agent
identity is `~/git/me` + secrets + network reach, not a specific machine.

Today's path — launch a Console session in `~` from iOS — is 20s+ of ceremony
per thought and exposes session mechanics the user shouldn't see.

### The structural insight

Shadow/Helm/Console derive their entire complexity from wrapping **foreign
agent harnesses** (Claude Code, Codex, Cursor, OpenCode) that Longhouse does
not control. That is the product wedge: people already have agents typing in
terminals, and Longhouse works with them. But it also means Longhouse is at
the mercy of those harnesses: no token stream, no context control, no model
choice, per-invocation prefill and spawn cost.

A companion sidecar has **no terminal to respect**. It is the first surface
free of the foreign-harness constraint — and it should spend that freedom on
latency, voice, and context engineering, NOT on rebuilding a coding harness.

### Verified primitives (as of 2026-07-19)

- **Turn-scoped Console execution is canon** (`turn-scoped-console-execution.md`,
  locked 2026-07-14): Console persists conversation *threads*, not idle
  provider processes. Five nouns: Session / Thread / Turn / Invocation /
  Adapter. FIFO turns, single execution owner, interrupt semantics, typed
  adapter-unavailable results.
- **Native LLM client lanes exist in prod** (`config/models.json`,
  `models_config.get_llm_client_for_use_case`): OpenRouter-backed tiers,
  including a low-latency `gemini-flash-lite:nitro` lane already live for
  session titles.
- **Builtin tool registry exists** (`server/zerg/tools/builtin/`, contracts
  generated from `schemas/tools.yml`): session tools, session-coordination
  tools, memory tools, web search/fetch, MCP adapter. No active native agent
  loop consumes them today.
- **Server-side turn execution has precedent**: `session_chat` spawns a
  `claude` CLI subprocess on the Runtime Host (SESSION_CHAT_BACKEND) to fake
  exactly this capability. A native adapter is its one-path replacement, not
  a parallel stack.
- **No assistant-delta stream exists for foreign-harness sessions** (iOS SSE
  is workspace invalidation; Claude channel is input-only). For a *native*
  invocation this constraint disappears: we own the loop, we emit deltas.

## Goals

1. End-of-speech → audio-start ≤3s for conversational turns, engineered (not
   hoped): streaming tokens, bounded context, prompt caching, no process
   spawn per turn.
2. One durable companion thread; the client never exposes session mechanics.
3. Full personal context via tools + curated prompt, equal on any qualified
   home base (clifford first).
4. Heavy work is **dispatched to the fleet** (foreign-harness sessions via
   existing turn dispatch / hatch), never executed inside the voice loop.
5. Everything rides the existing pipeline: thread/turn kernel, ingest,
   archive, search, recall, capabilities, iOS surfaces.

## Non-goals

- A Longhouse coding harness. The native adapter gets NO Bash/Edit/file
  tools. If a request needs repo mutation or long tool chains, the companion
  dispatches it to a real provider session and says so. This boundary is
  what keeps the launch story ("works with your existing agents") intact.
- A fourth session mode. The companion is a Console thread whose Adapter is
  native. Taxonomy holds. ("Hearth" stays reserved if a resident primitive is
  ever truly needed; current canon suggests it is not — threads, not
  processes, are the durable thing.)
- Realtime speech-to-speech models (breaks the text-thread contract that
  makes the pipeline valuable).
- Multi-companion, other-user generalization, barge-in. Dogfood first.

## Design

### Native Adapter (new)

A turn executor implementing the existing Console turn contract, running on
the **Runtime Host** (where durability lives; hosted david010 for David):

- Claims a queued Turn under the normal execution-owner lease; runs an LLM
  tool-loop (client from `models.json` lane `companion`, fast non-reasoning
  model) against the thread's bounded context; settles the turn through the
  normal state machine (`queued → starting → active → draining → completed`).
- Emits **incremental assistant deltas** to a streaming sink as first-class
  runtime events; whole events land in ingest/archive exactly like any other
  session. Foreign-harness sessions stay whole-message; native turns stream.
- Tool allowlist (curated, small): recall/search sessions, session
  coordination (create/dispatch turns on other threads, steer managed
  sessions), life hub evidence, notify/push, web search, memory. All via
  existing builtin registry + `/api/agents/*` semantics.
- Context policy is owned code: system prompt = companion skill (voice
  register, delegation posture, consequential-action gate) + distilled
  personal context; bounded rolling history + provider prompt caching;
  recycle-with-summary is an adapter concern, invisible to the client.

### Machine reach

The adapter runs server-side, but machine work flows through the existing
machine surface: dispatching a coding task = creating a Console turn on a
thread whose adapter is a foreign harness on clifford; steering = existing
send paths. The companion needs clifford to be a qualified agent home
(synced `~/git/me`, machine token, Machine Agent, hatch) for dispatched work,
not for its own conversational loop.

### iOS Talk surface

- One button → full-screen voice UI bound to the companion thread (server
  resolves "the companion thread" for the account; no picker, no session id).
- On-device streaming STT (Apple Speech; authorization/locale checks, honest
  degraded state, never silent server fallback).
- Sentence-boundary TTS over the delta stream (v0 AVSpeech → v1 streaming
  API voice if it grates).
- Later: App Intents ("Hey Siri…"), widget entry.

## Tradeoffs

**Accepted costs**

- Real product code pre-launch (adapter, streaming sink, Talk surface) while
  the wedge demo still needs hardening. Mitigations: the adapter replaces the
  `session_chat` claude-subprocess hack (debt already owed); the streaming
  sink is scoped to native turns only; slices are gated so each ships value
  alone.
- A second answer-quality surface to keep honest: the companion can be wrong
  in ways a wrapped Claude session wouldn't be (smaller model, curated
  tools). Accepted for conversational/dispatch use; bounded by the non-goal
  wall (no coding tools).
- Server-side execution means hosted-instance secrets (OpenRouter key, life
  hub access) and a 24/7 personal-data-capable endpoint reachable from a
  phone. Consequential-action gate lives in the companion skill from day one;
  device auth is the existing iOS auth path.

**Rejected alternatives**

- *Invisible-Helm pty hack* (v1 of this spec): inherits every foreign-harness
  constraint — no deltas, no context control, per-turn prefill, fragile pty
  lifecycle — and contradicts turn-scoped canon (idle processes are not
  session identity). Zero-code was its only virtue and it spent that virtue
  on the wrong bottleneck.
- *Foreign-harness Console turns as the companion* (e.g. `claude -p` per turn
  via the codex_exec-style adapter): canon-compliant but voice-hostile:
  process spawn + full prefill per turn, whole-message replies, no latency
  ceiling engineering possible.
- *Own coding harness*: strategic treadmill; violates the wedge. Explicit
  non-goal.

## Slices

0. **Native adapter, text only, existing UI.** Adapter + `companion` models
   lane + tool allowlist behind the existing Console composer (web/iOS as-is,
   whole-message). Proves turn mechanics, tool loop, answer quality, and
   measures server-side TTFT honestly. Gate: daily-driver useful in text.
1. **Delta streaming sink** for native turns + latency instrumentation
   (p50/p95: input-accepted → first delta → first sentence).
2. **iOS Talk surface v1**: STT → companion thread → sentence-boundary TTS
   over the stream.
3. **Dispatch polish**: delegation contract (task identity, completion
   notification, readback of finished background work).
4. **Entry polish**: App Intents, widget, TTS voice upgrade.

## Open questions

- Model lane for `companion` (fast non-reasoning; gemini-flash-lite-class vs
  Cerebras-class throughput) — pick from measured TTFT after slice 0.
- Thread recycle policy (size-based summary rollover) — decide from slice-0
  context-growth data.
- Where the companion thread's durable "home" is when self-hosted (Runtime
  Host is always the answer; hosted david010 for dogfood).
- Whether the native adapter later subsumes `session_chat` continuation
  entirely (one-path consolidation) — likely yes, separate effort.
