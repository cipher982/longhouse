# Companion — durable concierge thread, text-first, voice later

Status: draft v3 (2026-07-19). Synthesis of three independent reviews
(adversarial architecture, steelman, product/scope) of the v2 native-adapter
draft. v3 changes the execution choice and sequencing; the destination
(one durable concierge thread, zero-ceremony phone access) is unchanged.

## Context

David wants a zero-friction, on-the-go way to talk to an agent with his full
context (global AGENTS.md, skills, life hub, email, repos): reach it from the
phone with no session ceremony, ask, get an answer, walk away. "Ephemeral
Siri question" and "long-running thread" collapse into one durable thread
with zero per-interaction setup. Host must be always-on; cinder is fungible
computation — agent identity is `~/git/me` + secrets + network reach.

Structural insight that survives review: Shadow/Helm/Console complexity is
the tax of wrapping foreign harnesses, and a companion has no terminal to
respect. But review established that escaping the foreign harness means
building a **second execution kernel** (executor ownership, transcript
authority, delta convergence — none exist server-side), and that bill must
not be paid on speculation, pre-launch, when the planned foreign-harness
path may hit the bar.

## Decision

**Companion v0 is a durable Console thread executed by the planned
`claude_print` turn adapter** (stream-JSON + provider-native `--resume`,
Machine Agent-owned, per turn-scoped canon phase 2) on clifford, driven
text-first from the existing iOS composer.

Why this wins the synthesis:

- **Canon-compliant, no second kernel.** The Machine Agent owns the
  invocation; the provider's durable transcript is the convergence
  authority; turn lease/settle semantics are the existing ones. Every FATAL
  from the architecture review applies to the native path, none to this.
- **The dogfood pays for the wedge.** `claude_print` is launch-critical
  turn-scoped adapter work regardless of the companion. Companion v0 rides
  it rather than competing with it for pre-launch time.
- **No second answer-quality surface.** v0 is real Claude with the real
  AGENTS.md, skills, and tools — not a curated mini-me on a flash model.
- **Deltas are not exclusive to the native path.** Stream-JSON gives
  incremental output on the foreign-harness path; provider prompt caching
  on `--resume` attacks prefill. Whether that lands in budget is a
  measurement, not an assertion — see gates.

**The native adapter is demoted to a gated hypothesis**, not a plan. It gets
built only if all of: (a) the cached-resume baseline fails the latency or
context-policy bar after a real week of use; (b) post-launch; (c) a locked
executor-and-convergence contract exists first (execution-owner lease and
placement for server-side turns, append-only native transcript authority,
delta identity/replay/backpressure, drain barrier before lease release).
Its strongest surviving justification is owned context policy (bounded
history, recycle-with-summary, distilled personal context) — if provider
mechanisms (CLAUDE.md, append-system-prompt, scheduled summarization turns)
prove sufficient, the hypothesis dies.

## Goals (v0)

1. Zero-ceremony access: server-resolved "the companion thread" — no
   machine picker, no session picker, no new-session flow on the client.
2. Full personal context: companion skill in `~/git/me` (register,
   delegation posture, evidence-layer routing) + the real agent home.
3. Success metric: **used from the phone in text 3+ times/day for a week
   while away from the laptop.** Not TTFT.
4. Instrumented baseline: p50/p95 per stage (input accepted → invocation
   start → first delta → turn settle) captured from day one, because it is
  the kill/keep evidence for the native-adapter hypothesis.

## Non-goals

- Voice in v0. Voice is phase 2, gated on v0 stickiness, and enters through
  **system entry points first** (App Intents/Siri, AirPods, lock screen /
  Action Button); an in-app Talk screen is a debug harness, not the product.
  No ≤3s end-to-end promise — the honest voice posture is instant spoken
  ack + answer when ready, with tool-turns allowed to take their time and
  land as push + text when the phone locks.
- A Longhouse coding harness. Unchanged.
- A fourth session mode. Unchanged; the thread is Console.
- Realtime speech-to-speech. Unchanged.

## Trust boundary (blocks any phone-reachable phase)

- Action classes enumerated in code — read-only / notify / dispatch-mutating
  / external-consequential — enforced at the tool-invocation point, never in
  the system prompt.
- Consequential actions require on-device biometric/passcode confirmation
  (voice confirmation is worthless; a stolen unlocked phone has a voice).
- Prompt-injection posture: the companion reads email and the web while
  holding dispatch power over the fleet. Untrusted-content marking and "no
  consequential action sourced from tool-result text alone" are enforced
  invariants, not prompt requests.
- If a native adapter ever exists, its tool line is **read-only vs
  mutating** — read-only inspection is allowed (a concierge that dispatches
  a coding session to run `ls` is a UX failure); mutation always dispatches.

## Phases

0. **Companion thread on `claude_print`** (rides turn-scoped phase 2):
   thread + companion skill + clifford qualified as agent home; existing
   iOS composer; per-stage latency instrumentation; one week of honest use.
1. **Gate review**: stickiness (3+/day) decides whether anything more gets
   built; latency + context-policy findings decide the native-adapter
   hypothesis (kill by default).
2. **Voice via system entry**: App Intent → companion turn → spoken ack →
   TTS answer when settled, push + text on lock. Failure-mode matrix
   (STT misfire read-back before dispatch, mid-turn host loss, degraded
   provider) specified before build.
3. **Only if earned**: delegation contract (task identity, completion
   push, readback), richer voice, native-adapter contract work.

## Dropped from v2

- Native adapter as the v0 execution path (second kernel; three FATALs).
- The `session_chat` consolidation justification (factually stale — no
  subprocess backend exists in current code; the draft-reply generator is
  a separate, toolless surface).
- The ≤3s end-of-speech→audio budget (unmeasurable as promised; STT
  finalization + tool rounds break it; replaced by ack-latency posture).
- In-app Talk screen as the voice entry (system entry points are the
  actual mobile product).

## Open questions

- Does cached-resume TTFT on clifford land under ~3s for no-tool turns?
  (Measured in phase 0; this number decides the native hypothesis.)
- Thread recycle mechanics on the foreign-harness path: is scheduled
  summarize-and-restart via a normal turn good enough? Does dropped history
  remain reachable via recall over the archive (it should — say so when
  verified)?
- Concurrent access semantics (phone + web composer on one thread): FIFO
  exists; the UX stance (queue-and-say-so) needs a decision before voice.
- Cost/observability: per-turn cost telemetry as runtime events + a budget
  circuit breaker before any 24/7 proactive behavior exists.
