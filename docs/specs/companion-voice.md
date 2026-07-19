# Companion Voice — spec

One durable agent thread ("the companion") on a 24/7 home-base machine, driven
by a one-tap voice screen on iOS. Siri-feel latency for conversation; heavy
work delegated off the voice path. **v0 ownership statement: this is a managed
Helm session retained in a detached pty** — not Console (one-shot per turn),
and not a new session architecture.

## Problem

Talking to a fully-contextualized agent (AGENTS.md + skills + life hub +
repos) from the phone today means launching a Console session in `~` via the
iOS app: pick machine, spawn session, wait for cold start, read a transcript.
That is 20s+ of ceremony per thought. The desired interaction is: tap, talk,
hear an answer in ~2s, walk away. Cinder (laptop) cannot be the host — it is
closed in a backpack; the host must be an always-on box (clifford/cube), which
means machine fungibility must actually be true, not aspirational.

## First principles

1. **Voice loop ≠ agent loop.** Voice needs 1–3s turns; agent turns take
   10s–minutes. The companion answers directly with a fast model and
   *delegates* heavy work asynchronously (via hatch), narrating the handoff.
2. **Ephemeral vs long-running is a false split.** One durable thread with
   zero per-interaction setup gives both. "Ephemeral" = a quick question to
   the standing thread. Memory across the day is a feature, not a cost.
3. **Machine fungibility is provisioning, not architecture.** Agent identity =
   `~/git/me` checkout + Infisical machine token + Tailscale reach. Any box
   with those qualifies. Longhouse's machine abstraction already routes to it.

## Design

### Companion session
- One persistent managed session on the home-base machine (default:
  clifford). Long-lived process, NOT `run_once` per turn — per-turn spawn
  latency can never feel like Siri.
- **Mode reality check:** a "persistent headless steerable session" is not a
  primitive today — Helm is persistent (TUI in a pty), Console is one-shot.
  v0 uses **invisible Helm**: `longhouse claude` with a fast model in a
  detached tmux/pty kept alive by launchd/systemd on clifford. Fully managed,
  steerable, zero new product code. A true headless-persistent mode is a
  later refactor only if the pty hack proves fragile.
- Runtime: stock `claude` CLI with a fast non-reasoning model (haiku-class).
  Reuses skills, tools, and the shipped steer/send path wholesale. A bespoke
  API loop is explicitly rejected for v1 (second agent runtime, one-path rule).
- System prompt: global AGENTS.md + a `companion` skill in `~/git/me` defining
  voice register (short spoken answers), delegation posture (hand slow work to
  hatch and say so — no numeric threshold; a prompt-level time rule is
  unreliable), and what evidence layers to reach for (life hub, recall).
  v0 delegation has no result-routing contract: fired work is simply
  mentioned, and results come up next time you ask. A real delegation
  contract (task identity, completion notification, readback) is deferred.
- Voice turns are steer messages into this session. Nothing new on the send
  path — Epic 1 shipped it.

### Lifecycle (deliberately manual in v0)
- launchd keeps the tmux/pty alive; David restarts the session manually when
  context gets fat; iOS pins the session manually after a restart.
- A server-side `companion` alias (stable name → current live session id) and
  an automatic recycle-with-summary policy are **deferred until manual
  re-pinning has actually annoyed us** and we have real data on context
  growth. Do not build supervisor product code on day one.
- Safety: the companion skill carries the consequential-action gate from day
  one — a 24/7 agent with full personal-data reach, steerable from an
  unlocked phone, must confirm before purchases/portal actions/irreversible
  side effects.

### iOS Talk screen
- A dedicated button → full-screen voice UI. No machine picker, no session
  picker, no "new session" — the button binds to the `companion` alias.
- STT: on-device Apple Speech framework, streaming. Needs authorization +
  locale/device availability checks and an honest degraded state; must never
  silently fall back to server transcription.
- TTS: v0 AVSpeech (zero dependency) → v1 streaming API voice (OpenAI TTS or
  ElevenLabs) if AVSpeech grates.
- Reply path v1: **whole-message granularity is the reality.** Today's iOS
  SSE is a workspace-invalidation feed with transcript preview, not a
  token/transcript protocol, and the Claude channel is an input-only control
  seam (send/interrupt/steer). There is no assistant-delta source anywhere in
  the stack today. v1 therefore TTS's the completed assistant message as it
  lands via ingest.

### Streaming relay (contingent, and currently source-less)
- If measured latency blows the budget, the fix is NOT "add a relay" — it is
  first a **spike to find where incremental assistant text can be tapped**
  (engine-side transcript tail? provider stream inside the Helm wrapper?),
  then the thinnest relay on top of whatever that spike finds. Ingest/archive
  unchanged either way. Do not design this until the spike runs.

## Latency budget — a hypothesis, not a fact

Target: **≤3s end-of-speech → audio starting**. Current console flow: 20s+.

The weakest assumption is fast-model first token in 0.5–1s: the companion
carries global AGENTS.md + skills + a growing thread, and a warm CLI process
does not imply provider-side KV/prompt-cache persistence — prefill cost is
paid per turn. Whole-message reply granularity (above) further delays "first
sentence" by full generation time.

**Spike gate before any UI work:** instrument, on the actual home-base host,
p50/p95 for each stage — end-of-speech, transcription final, input accepted,
provider first output, first usable assistant sentence, client receipt, TTS
first audio. The measured numbers, not this budget, decide whether the relay
spike happens and whether the model/harness choice must change.

## Out of scope (v1)

- Siri/App Intents entry ("Hey Siri, ask Longhouse…") — phase 2 polish.
- Barge-in / full-duplex audio.
- Realtime speech-to-speech models (breaks the text-session contract that
  makes Longhouse the backbone here).
- Multi-companion / per-project companions. One thread.
- Any generalization to other users' machines. This is dogfood-first.

## Implementation slices

0. **Zero-code dogfood loop** — provision clifford (synced `~/git/me`,
   machine token, Machine Agent, hatch; Agent Home Epic work), start
   invisible-Helm companion under tmux+launchd, write the `companion` skill,
   drive it from the **existing iOS session reply UI with the dictation
   keyboard**. This validates the control path, textual turn latency,
   machine fungibility, and daily usefulness — NOT the audio loop (the app
   has no STT/TTS today). Gate: if the text loop doesn't feel good after a
   few days, stop and rethink — voice polish won't save a bad loop. Includes
   the latency spike-gate instrumentation from the budget section.
1. **iOS Talk screen v1** — dedicated button bound to the pinned companion:
   on-device STT → steer → tail → TTS. First real product code.
2. **Measure, then maybe relay** — instrument end-of-speech → first-audio;
   build the streaming relay only if the number demands it.
3. **Lifecycle hardening** — `companion` alias + recycle policy, only once
   manual re-pinning has demonstrably annoyed us.
4. **Polish** — App Intents ("Hey Siri…"), better TTS voice, delegated-task
   status readback ("your two background tasks finished").

## Failure & recovery (v0 answers, deliberately crude)

- Host reboot: launchd restarts tmux + session; new session id; re-pin in iOS.
- Stale pinned session / expired iOS auth: existing app behavior; re-pin or
  re-auth manually. Acceptable until it annoys.
- Steer during a busy active turn: existing steer semantics apply; the
  companion skill tells the model to acknowledge queued input briefly.

## Decisions

- Home-base machine: **clifford** (decided 2026-07-19). Second home base
  (cube) only after slice 0 proves the loop.
- Model + harness: researched 2026-07-19 (exa landscape, primary sources).
  Latency/cost winner: **Gemini 2.5 Flash-Lite via OpenRouter** (0.38s p50
  TTFT, 75 tok/s, $0.10/$0.40 per M on Vertex-EU route; do NOT use `:nitro` —
  it sorts by throughput and overrides tool-quality routing). Runner-up:
  Cerebras `gpt-oss-120b` (~3,000 tok/s, generation effectively free after
  first token). **But both need OpenCode as harness, and OpenCode is not a
  managed Longhouse provider today** — choosing them makes "add managed
  OpenCode support" a prerequisite, killing the zero-code slice 0.
  **Call: slice 0 runs Haiku 4.5 via `longhouse claude` on Bedrock** (native
  managed path, ~0.5–1s TTFT observed, works today) and treats the
  Flash-Lite/OpenCode switch as a measurement-gated upgrade — justified only
  if slice-0 numbers say model TTFT (not ingest/reply-path lag) is the
  bottleneck. Adding managed OpenCode may be strategically worthwhile for
  Longhouse regardless; that's a separate product decision.
- TTS voice: decide after slice 1 dogfood, not before.
