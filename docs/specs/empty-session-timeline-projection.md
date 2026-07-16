# Empty Session Timeline Projection

## Problem

Longhouse creates the durable session shell before a Console user submits the
first turn. Helm launch attempts can also create a shell before the provider
records transcript content. Today those zero-content rows fall through the
headline resolver to the project name, so several abandoned shells appear as
indistinguishable rows such as `davidrose` or `g55`. They look like failed AI
title generation even though there is no prompt for the title model to name.

## Product Contract

An empty session is a projection of existing facts, not a stored session type.
All of these conditions must hold:

- zero user, assistant, and tool-message counts
- no usable durable first-user-message preview or AI anchor title
- the session has not been tombstoned or deleted

A missing preview alone is insufficient: content projection can lag, and some
durable user inputs are not text-titleable.

While a newly created empty session is eligible for the default timeline, its
headline is explicit:

```text
<project> · Empty session
```

If no useful project label exists, use `<Provider> · Empty session`. Its title
state remains `awaiting_input` and its title source remains the existing
`project` contextual-fallback value, including when the provider supplies the
visible label. It is not title-generation debt and must not invoke the title
model.

The first durable user message ends the empty projection immediately. The
normal title ladder then applies: prompt-derived temporary headline followed by
the near-real-time AI anchor title.

## Default Timeline Visibility

Empty sessions remain subject to the same default-timeline eligibility,
filtering, ordering, and pagination rules as other human-started sessions. This
correction does not infer abandonment from age or runtime freshness and does
not automatically hide empty sessions. Explicit deletion, tombstoning, or an
existing persisted visibility decision may hide them.

## Client Behavior

Web, iOS, menu bar, and future clients consume the server-resolved
`timeline_title`; they do not independently decide whether title generation is
broken. Timeline omission must never close an already-open detail route.

The preferred future creation flow is client-local draft state followed by
durable session creation on first send. That removes empty server shells
entirely, but it is not required for this correction because launch target and
capability negotiation currently need a durable session ID.

## Edge Cases

- A user may compose indefinitely without timeline eligibility changing. The
  first durable user message atomically ends the empty projection.
- Failed or abandoned launch cleanup is outside this correction and requires
  an explicit persisted visibility policy.
- Image-only or otherwise untitleable sessions are not empty once a durable
  user message exists. They follow the existing title exemption behavior.
- Counts and pagination describe the projected default timeline, not hidden
  empty shells.

## Acceptance Criteria

1. A new empty Console session appears promptly as
   `<project> · Empty session` on every client.
2. It never enters the AI-title retry/backfill queue before meaningful user
   input exists.
3. Its first durable user message restores the normal prompt-to-AI title flow.
4. An empty session is not hidden solely because of age or uncertain runtime
   state.
5. Timeline list, direct session reads, machine-session deltas, counts, and
   pagination apply one server-owned projection.
6. Existing titled and title-exempt sessions are unchanged.
