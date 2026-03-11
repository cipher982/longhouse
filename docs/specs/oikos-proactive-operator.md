# Oikos Proactive Operator

Status: active dogfood
Owner: David / Oikos product direction
Updated: 2026-03-11

## Problem

Today Oikos is mostly reactive. It answers when asked, but it does not yet feel like a real technical deputy that can watch active coding work, notice meaningful changes, decide what matters, and act or escalate on its own.

That is the real product direction. Cleanup/refactoring was only a prerequisite so we can add this behavior without building on a mess.

## Working Framing

Names like `Session Shepherd` are only placeholders. Do not overfit the product to a nickname.

The durable idea is:

- Oikos is a proactive operator, not just a chat box
- it can wake up from meaningful triggers or periodic sweeps
- it can inspect durable state, decide what matters, and take bounded actions
- it can escalate when the right move is to involve the user

## Current State

As of 2026-03-11, the first dogfood ring is no longer hypothetical. The repo now has:

- a transport-agnostic `invoke_oikos()` seam plus a dedicated `operator` surface
- a deterministic shadow journey harness with durable artifacts
- live wakeups for:
  - `presence.blocked`
  - `presence.needs_user`
  - `periodic_sweep`
  - recent `session_completed` after post-ingest summary succeeds
- thin user-backed operator policy in `User.context["preferences"]["operator_mode"]`

What is still missing:

- broader proactive action types beyond session continuation
- browser / hosted smokes for the live operator loop

That means the current state is best described as:

- real trigger wiring exists
- real policy gating exists
- evaluation coverage exists
- wakeup history is now explicit enough to review suppressed / ignored / acted / failed outcomes
- the first bounded continuation action path exists, but broader actions are still future work

## Product Principles

### 1. Oikos should be allowed to decide, not just react

The point is not to build a bigger rules engine. The point is to let a strong model look at the current situation and decide what to do next.

Triggers, policies, and tools should wake and empower Oikos. They should not replace its judgment with a maze of if/else automation.

### 2. Durable state matters more than a single immortal thread

The important state should live in durable artifacts:

- coding-agent session transcripts
- Oikos messages and summaries
- trigger history
- policies / preferences
- tool outputs and artifacts
- decision logs

Oikos should be able to wake up cold, reconstruct what matters, and continue. The product must not depend on one giant always-hot thread carrying the whole world in prompt context forever.

### 3. Longhouse already owns the hard part: session history

Longhouse already stores the coding-agent history that matters. For this feature, the new state to introduce should stay small and Oikos-specific.

That means:

- do not duplicate the agent transcript/archive model
- reuse session history as the primary evidence layer
- add only the minimum Oikos-owned state needed for wakeups, policies, and action history

### 4. Triggers should wake intelligence, not encode behavior

The system should react to meaningful events, but the event itself should not hardcode the action.

Good:
- `session completed -> wake Oikos -> decide`
- `session blocked -> wake Oikos -> decide`

Bad:
- `session completed -> always notify`
- `session blocked -> always continue`

Wakeups create a decision opportunity. Oikos may still choose to do nothing.

### 5. Small calls and big calls are different tools

Not all LLM calls are equal in cost or value. The product should be able to use:

- cheap small decision calls
- medium inspection / intervention calls
- rarer deep synthesis calls

This is an implementation choice, not a product constraint. As models improve, the system should be free to shift toward fewer larger calls without a redesign.

### 6. Filesystem and artifacts are working memory

Large outputs, logs, and intermediate state should live in durable artifacts rather than being stuffed into prompt context by default.

The model should be able to inspect, search, grep, jq, tail, summarize, or reopen those artifacts as needed.

This keeps Oikos grounded in durable evidence instead of pretending model context is a database.

### 7. Bounded autonomy is better than fake omniscience

Oikos should be able to:

- inspect a session
- continue a session
- look up recent context
- perform small bounded repairs or follow-ups
- notify / escalate to the user

It should not pretend every ambiguity can be resolved automatically. Escalation is a feature, not a failure.

### 8. Design for better models, not current hacks

Avoid locking the product into 2026-specific workarounds.

Prefer:

- capability-oriented interfaces
- reconstructable context
- durable artifacts
- visible action history

Avoid:

- deeply prescriptive orchestration trees
- narrow hardcoded trigger semantics
- architecture that only makes sense if models stay weak

## Non-Goals

- No giant schema-first rewrite before dogfooding
- No requirement for a nonstop token-generation loop
- No assumption that one always-growing prompt thread is the final architecture
- No hardcoded automation maze that decides everything before Oikos sees it
- No duplication of the existing coding-agent session archive

## Wake Model

Oikos should be wakeable in more than one way:

- meaningful session transitions
- periodic sweep / fallback checks
- explicit user requests
- future system events (CI, deploys, runners, etc.)

The exact trigger set should stay intentionally small at first. The principle is more important than the full matrix.

The default mental model is:

1. something meaningful happens
2. Oikos wakes up
3. Oikos inspects durable state
4. Oikos decides whether to act, wait, or escalate

## V0 Runtime Contract

The current v0 dogfood runtime should be thought of as a narrow, explicit contract rather than a vague "autonomy layer."

### Current wake sources

The wakeup set should stay intentionally small for now:

- `presence.blocked`
  - generated from Claude Code permission / blocked states
  - deduped when the state and blocked tool have not materially changed
- `presence.needs_user`
  - generated from user-attention notifications
  - used as a decision opportunity, not an automatic ping
- `periodic_sweep`
  - builtin fallback job
  - meant to catch anything the event-driven paths miss
- `session_completed`
  - emitted only after transcript ingest succeeded and the durable summary worker finished
  - only for recent completions, not historical backfill

### Current guardrails

The current runtime intentionally avoids several tempting but unsafe triggers:

- do not wake directly on raw `idle` / Stop hooks
  - those can arrive before transcript ship / ingest / summary work settles
- do not treat every ingest with `ended_at` as a meaningful completion
  - `ended_at` advances on every shipped turn, not only on a final close
- do not wake on stale historical sessions
  - post-ingest completion is freshness-gated
- do not wake if fresh presence already says the session resumed or is paused in a more specific state
  - `thinking`, `running`, `blocked`, and `needs_user` suppress the completion wakeup

### Current execution path

The current end-to-end flow is:

1. a trigger occurs
2. the trigger builds a small operator wakeup message plus structured payload
3. the `operator` surface feeds the normal `invoke_oikos()` path
4. Oikos runs inside the existing run / event lifecycle
5. Oikos may still decide to do nothing

This is important: wakeups are not actions. They are invitations to inspect and decide.

## V0 Policy Surface

The thinnest current policy contract is:

- global master switch: `OIKOS_OPERATOR_MODE_ENABLED`
- per-user override: `User.context["preferences"]["operator_mode"]`

Current shape:

```json
{
  "preferences": {
    "operator_mode": {
      "enabled": true,
      "shadow_mode": true,
      "allow_continue": false,
      "allow_notify": true,
      "allow_small_repairs": false
    }
  }
}
```

Interpretation:

- the env var is the coarse operator kill switch
- the user preference is the fine-grained owner policy
- the additional booleans define the intended bounded action envelope even when some actions are not wired yet

This is enough policy for dogfood without inventing a dedicated autonomy settings table.

## Context Model

Oikos should not require the full raw transcript for every wakeup.

Its default operating context should be compact and reconstructable:

- what triggered the wakeup
- which sessions / threads are relevant
- latest visible state
- recent summary or last few turns
- current policy / user preference
- pointers to richer artifacts when needed

When Oikos needs more, it should drill in deliberately rather than hauling everything into every call.

## V0 Context Contract

The current wakeup payload should stay compact and reconstructable. At a minimum it should carry:

- trigger type
- session id when a session is implicated
- provider / project / cwd when available
- fresh presence state when relevant
- recent completion metadata such as `ended_at`
- summary title or compact summary when available

The prompt message can be human-readable, but the payload should remain structured enough that future tooling can log, dedupe, and inspect it without reparsing prose.

## Dogfood Direction

Start simple.

The first dogfood version should prove only that:

- Oikos can wake on a few meaningful coding-session events
- it can inspect the current situation
- it can choose between wait / act / escalate
- it can take one or two bounded follow-up actions
- the user can see what happened and decide whether the behavior is useful

This should feel like a smart technical personal assistant, not an enterprise workflow engine.

## Likely First Actions

The initial bounded action surface should stay small:

- inspect active / recently changed sessions
- read the latest relevant session context
- continue a resumable session
- notify the user that a real decision is needed

Anything more complex should be earned by dogfood evidence, not assumed upfront.

## Next Thin State

The next Oikos-owned state should not be another transcript store or a giant autonomy schema. It should be a tiny wakeup ledger.

Purpose:

- make wakeups reviewable even when no run is started
- make duplicate / suppressed / failed wakeups visible without mining full runs
- give us a stable place to attach later decision outcomes once bounded actions exist

Phase 1 shape:

- `owner_id` nullable
- `source`
- `trigger_type`
- `session_id` nullable
- `conversation_id` nullable
- `wakeup_key` nullable
- `status`
  - `suppressed`
  - `enqueued`
  - `failed`
- `reason` nullable
- `run_id` nullable
- compact structured `payload`
- `created_at`

Phase 1 scope:

- record trigger handling only
- persist why a wakeup was skipped, accepted, or failed before the run could be trusted
- link successful wakeups to the created `run_id`

Explicitly out of scope for phase 1:

- inferring `ignored` or `acted` from live runs before the first bounded action path exists
- turning the ledger into a second copy of run events or transcripts

Phase 2 can extend the same ledger with post-run decision outcomes such as:

- `ignored`
- `acted`
- `escalated`

Important constraint:

- this ledger should track wakeup handling, not become a second run log
- full execution detail still belongs in existing Oikos runs / events
- this table exists because "no run happened" is still product-relevant history

## First Bounded Action Slice

The first live action should stay narrower than the full vision.

Recommended first action:

- continue one resumable coding session when:
  - the session is clearly resumable
  - the transcript or summary points to one bounded next step
  - policy allows continuation
  - no stronger pause-state signal supersedes it

Not yet for the first slice:

- multi-session arbitration
- automatic user notifications as the default path
- free-form repairs with broad scope
- anything that requires inventing a larger orchestration framework first

## What Must Stay Visible

If Oikos wakes and acts, the user should be able to answer:

- what woke it up
- what it looked at
- what it decided
- what action it took
- where it chose to wait or escalate

The product should feel agentic, not mysterious.

## Open Questions

- Is there one long-lived Oikos operator thread per user, or one main thread plus lightweight side threads for specific interventions?
- Which additional transitions beyond the current wakeup set are worth adding next?
- When Oikos sees a completion, should the default be inspect-and-wait or inspect-and-continue?
- How much autonomy should be policy-driven vs implicit from user behavior once the thin explicit policy surface exists?
- When does Oikos directly act on a coding session vs spawn/delegate a separate worker flow?

## Acceptance For The Spec Phase

This spec is successful if it keeps us out of a hole.

That means:

- the direction is clear: proactive operator, not bigger chatbot
- the principles are explicit
- we avoid premature schema / trigger / loop lock-in
- the first dogfood slice is small and learnable
- future models can make the system simpler rather than obsolete
