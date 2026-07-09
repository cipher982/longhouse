# AI Title Reliability for Sessions

Status: Proposed
Last updated: 2026-07-09

## Decision

Longhouse should give every eligible human-started session an AI-generated,
stable title. This is a product reliability requirement, not best-effort
presentation polish.

`anchor_title` is the durable title truth. It may be written only by:

1. the initial-title model, using the first durable non-warmup user message;
2. a future explicit user title override; or
3. a future provider-native title accepted by an explicit policy.

It must not be written from a drifting transcript summary, assistant text,
tool output, a live/provisional preview, or a workspace/project fallback.

## Why

The menu bar exposed an unhealthy ambiguity: a session with no AI title could
render its workspace (`zerg`, `davidrose`) in the title position. That is useful
context, but it disguises title-pipeline failure as a plausible title. A gateway
outage, worker crash, or provider-ingest regression could therefore leave all
sessions untitled without a visible operational signal.

Fallbacks are recovery behavior only. They may keep a UI legible while the
title pipeline heals, but may never satisfy the product obligation to title an
eligible session.

## Scope

An eligible session has at least one durable, meaningful `AgentEvent` with
`role=user`. Test/e2e/provider-proof sessions are excluded. Autonomous
sessions with no user intent are explicitly exempt; their project/workspace is
context metadata, not a generated title.

The authoritative input is durable user intent. In particular, current Codex
live previews often contain partial assistant output, so they must never feed
title generation.

## Contract

The session API must expose a resolved title plus its state and provenance:

```json
{
  "timeline_title": "Refactor Graph Auth and M365",
  "title_state": "ready",
  "title_source": "ai"
}
```

`timeline_title` remains a server-resolved display string. Every client renders
it directly; clients do not decide whether a summary, prompt, project, or
workspace is a title.

States are:

| State | Meaning | UI behavior |
| --- | --- | --- |
| `awaiting_input` | Open session has no durable user intent yet. | Context label, clearly not a title. |
| `queued` / `generating` | An eligible session has a durable title obligation in flight. | Temporary sanitized prompt fallback, labelled as naming in progress. |
| `ready` | Durable AI/user/provider title exists. | Normal title. |
| `degraded` | An eligible session could not get an AI title before the title deadline. | Temporary fallback plus degraded provenance; retry remains durable. |
| `exempt` | No user intent is expected (autonomous/test/proof). | Context label, no title obligation. |

The initial compatibility implementation may return a conservative state for
older clients, but it must preserve the distinction between a title and a
fallback.

## Lifecycle

```text
first durable non-warmup user event
  -> persist title obligation
  -> immediately show a temporary prompt-derived fallback
  -> call title model outside the ingest transaction
  -> write anchor_title once
  -> publish title invalidation
  -> title_state=ready

model/gateway/worker failure
  -> title_state=degraded
  -> keep durable retry obligation
  -> repair worker retries
  -> title_state=ready only after anchor_title is written
```

The fast post-commit trigger minimizes normal latency. A persistent repair path
is the correctness mechanism: process restarts, in-memory task loss, model
outages, and missed provider edges must all converge without a new transcript
event.

## Invariants

- An eligible session with `anchor_title IS NULL` is title debt, even if it has
  a usable display fallback.
- `summary_title` is a drifting summary/search artifact. It never wins the
  race to populate `anchor_title`.
- The title worker is idempotent. Its write guard, not in-process dedupe, is
  the final authority.
- The model sees the first durable user prompt and small session metadata only.
- A session must never be named from assistant or tool output.
- `TimelineCard`, web, iOS, widgets, and macOS must agree on the same server
  title string and provenance.

## Implementation Plan

### 1. Make title ownership unambiguous

- Remove summary-driven writes to `anchor_title`.
- Keep `summary_title` for summary/search only.
- Remove unused transcript/assistant-inclusive title generation code.
- Ensure any timeline-card projection that can source a title mirrors
  `anchor_title`, not only `summary_title`.

### 2. Persist and expose title reliability state

- Add title state/source/error/retry metadata to the session projection.
- The first durable-user edge creates a durable title obligation.
- The reconciler selects title debt, including sessions currently showing a
  fallback. It is not allowed to treat that fallback as completion.
- Expose title debt in machine/product health.

### 3. Keep the live path honest

- Keep title-model calls outside ingest and send transactions.
- Use durable user events, never live transcript previews, as model input.
- Publish a session/timeline invalidation after a title write; refetch the
  canonical projection instead of shipping a patch payload.
- Client fallbacks are compatibility-only and must retain degraded provenance.

### 4. Repair existing data and enforce the SLO

- Backfill eligible untitled sessions from existing durable user events.
- Measure first-user-commit to AI-title-ready latency, title debt, retry age,
  title-model failures, and fallback/degraded counts.
- Alert on sustained eligible title debt; a UI that looks readable while title
  debt rises is not healthy.

## Acceptance Tests

- First durable user event schedules exactly one title obligation.
- A title-model outage leaves an eligible session retryable and degraded, not
  successfully titled by its project or prompt fallback.
- Recovery writes an AI title without another user message.
- A summary update cannot populate or replace `anchor_title`.
- Assistant-only live preview data cannot become an AI title.
- All title-bearing clients render the same `timeline_title` and state.
- `TimelineCard` cannot surface `summary_title` as the stable title when an
  `anchor_title` exists.
