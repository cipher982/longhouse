# Live Preview Ordering

Status: Draft
Last updated: 2026-07-08
Owner: Longhouse

## Summary

Longhouse can receive a live assistant `transcript_preview` over the workspace
stream before durable transcript ingest has caught up. If a user just submitted
input, the client can temporarily render assistant preview text before the
submitted user message appears in the durable REST tail.

That creates a false conversational order. It is related to interruption UX in
the screenshot report, but it is a separate streaming/pending-input bug from
session-action classification.

## Product Contract

Assistant preview text must not leapfrog a pending or newly submitted user
input.

If the client cannot prove preview ordering, it should render the preview as
live progress rather than as a finalized transcript row.

## Desired Rules

- If there is pending submitted/queued input not yet visible as durable user
  text, render that pending input before assistant preview.
- If the server can identify the triggering input or turn, include linkage in
  the preview payload.
- If linkage is unknown, clients should render the assistant preview as
  live-progress, not as a finalized transcript row.
- Once REST tail includes durable user and assistant rows, replace preview and
  pending rows with durable projection.

## Test Strategy

- iOS fixture: durable tail lacks the submitted user row, pending input exists,
  assistant preview arrives, visible ordering remains user then assistant.
- Web fixture: same case through `SessionChat` pending-input state.
- Server fixture: workspace stream includes preview linkage when available.

## Relationship To Session Actions

Session actions make interruption render correctly. Preview ordering makes live
conversation order truthful while archive ingest catches up. They should share
fixtures where useful, but neither fix should block the other.
