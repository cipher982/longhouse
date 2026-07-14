# Immediate Session Titles

Status: Accepted
Last updated: 2026-07-13

## Decision

The first durable, meaningful user message is the session title source.
Longhouse sanitizes it to a short headline and freezes that value immediately.

Session naming is a synchronous projection, not an AI enrichment job. It must
not depend on a model call, background queue, retry state, summary completion,
or client-side inference.

## Contract

When a durable user message exists:

```text
first durable user message
  -> sanitize to a short headline
  -> persist if the session title is empty
  -> expose the same timeline_title to every client
```

The write is idempotent and write-once. Later messages and transcript summaries
must not rename the session.

Before the first user message, the API may return workspace/provider context.
That is an empty-session label, not a naming workflow.

## Storage ownership

Catalogd/storage-v2 owns the durable title because it owns the durable user
message. The title is written in the same catalog transaction that publishes
the first rendered user message.

During the storage-v2 compatibility period, `summary_title` carries this stable
title value. It must be treated as write-once. A later schema cleanup may rename
the column to `title`; that rename is not required for the product behavior.

Existing storage rows with no persisted title resolve the same deterministic
title from `first_user_message_preview` at read time. This repairs display
immediately without a broad startup rewrite.

## Client behavior

`timeline_title` is the server-resolved display string. Web, iOS, widgets, CLI,
and macOS render it directly.

Clients must not replace a non-empty `timeline_title` with “Naming session…”,
workspace context, summary text, or local title-state logic.

## Invariants

- Title availability adds no network or model round trip beyond durable ingest.
- Assistant output, tool output, and later user messages cannot become the title.
- A title never changes implicitly after it first appears.
- One sanitization function defines title text for every provider.
- Model availability has no effect on session naming.
- All title-bearing clients render the same `timeline_title`.

## Acceptance tests

- The first durable user message produces a title in the same storage commit.
- Image markers, fences, URLs, and whitespace are sanitized consistently.
- A later render object cannot replace an existing title.
- An existing untitled storage row resolves from its first-user preview.
- A non-ready legacy enrichment state cannot hide a non-empty `timeline_title`.
