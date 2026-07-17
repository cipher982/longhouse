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

When read directly by ID, an empty session has an explicit diagnostic
headline:

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

Empty sessions are hidden from the default timeline from creation. A durable
session shell is implementation state used to negotiate launch target,
capabilities, and a stable detail URL; it is not user history until transcript
content exists.

The canonical server-owned `hidden_from_default_timeline` projection applies
to timeline lists, counts, search defaults, notifications, iOS, web, and menu
bar feeds. Clients must not reimplement emptiness or age rules.

The first durable transcript content atomically clears the hidden flag. For
Console this is normally the submitted user message. Helm and Shadow sessions
also become visible once any durable user, assistant, or tool content exists.
Visibility is evidence-based, never age-based: an empty shell stays hidden
indefinitely, while a non-empty session never becomes hidden merely because it
is old or inactive.

## Client Behavior

Web, iOS, menu bar, and future clients consume the server-resolved visibility
and `timeline_title`; they do not independently decide whether a shell is empty
or title generation is broken. Timeline omission must never close an
already-open detail route.

The preferred future creation flow is client-local draft state followed by
durable session creation on first send. That removes empty server shells
entirely, but it is not required for this correction because launch target and
capability negotiation currently need a durable session ID.

## Edge Cases

- A user may compose indefinitely in the open detail view. The draft is local
  client state; the hidden server shell remains directly retrievable, and the
  first accepted durable message reveals it across clients.
- Failed or abandoned launches with no transcript remain hidden without a
  cleanup timer. They may still be inspected by direct ID for diagnostics.
- A send accepted by the control path but not yet durable does not reveal the
  row prematurely; durability is the cross-client publication boundary.
- Image-only or otherwise untitleable sessions are not empty once a durable
  user message exists. They follow the existing title exemption behavior.
- Counts and pagination describe the projected default timeline, not hidden
  empty shells.

## Acceptance Criteria

1. A new empty Console or Helm shell is absent from default timeline lists and
   counts but remains retrievable directly as `<project> · Empty session`.
2. It never enters the AI-title retry/backfill queue before meaningful user
   input exists.
3. Its first durable user message restores the normal prompt-to-AI title flow.
4. The first durable transcript content clears
   `hidden_from_default_timeline` and publishes the session across clients.
5. Timeline list, direct session reads, machine-session deltas, counts, and
   pagination apply one server-owned projection.
6. Existing titled and title-exempt sessions are unchanged.
7. Existing zero-content shells are backfilled hidden; no transcript or session
   rows are deleted.
