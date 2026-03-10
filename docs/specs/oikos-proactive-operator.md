# Oikos Proactive Operator

Status: proposed
Owner: David / Oikos product direction
Updated: 2026-03-10

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
- Which session transitions are valuable enough to wake Oikos in v1?
- When Oikos sees a completion, should the default be inspect-and-wait or inspect-and-continue?
- How much autonomy should be policy-driven vs implicit from user behavior?
- When does Oikos directly act on a coding session vs spawn/delegate a separate worker flow?

## Acceptance For The Spec Phase

This spec is successful if it keeps us out of a hole.

That means:

- the direction is clear: proactive operator, not bigger chatbot
- the principles are explicit
- we avoid premature schema / trigger / loop lock-in
- the first dogfood slice is small and learnable
- future models can make the system simpler rather than obsolete
